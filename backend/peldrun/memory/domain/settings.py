# backend/peldrun/memory/domain/settings.py
"""
PELDRUN Memory Platform - Configuration & Token Budget Contracts.

Defines:

    * ``MemoryTokenBudget`` -- per-turn token allocation limits
      (max, user-scope reservation, warning threshold).
    * ``MemorySettings``    -- the complete configuration snapshot
      for the memory subsystem: mode, provider, retrieval tuning,
      budget, extraction policy, and per-project overrides.

Relationship to ``RunRequest``
------------------------------

**These settings are NOT part of any run.** They are loaded ONCE
during FastAPI ``lifespan`` initialization (P1.7) and held by the
``MemoryService`` singleton. A given run therefore observes the
settings that were current when the run started; a settings change
mid-run affects only the NEXT run. This is the design the plan's
section 0 mandates ("MemoryService... does NOT hold conversation
state"), and it is what acceptance test #9 verifies.

Because settings are decoupled from individual runs, they must be
a **pure value object**: no I/O, no repository references, no
callbacks. The whole model can be serialized to JSON, compared for
equality, copied, and cached without side effects. Persistence
lives entirely in ``MemorySettingsManager`` (P1.7).

``project_overrides`` semantics
-------------------------------

``project_overrides: Dict[str, MemorySettings]`` supports per-project
customization. The nested instances are DELTA-like: a field is
considered "overridden" if and only if the caller explicitly
supplied it. Fields the override did not set inherit from the parent.

This is exposed via ``effective_for_project(project_id)``, which:

  1. returns ``self`` when the project has no override,
  2. otherwise starts from ``self``'s values and overlays the
     override's EXPLICITLY-SET fields on top (using Pydantic's
     ``model_fields_set`` / ``model_dump(exclude_unset=True)``),
  3. returns a NEW ``MemorySettings`` instance with empty
     ``project_overrides`` (preventing any possibility of recursion).

An override MUST NOT itself contain ``project_overrides``. The
``_reject_nested_overrides`` model validator enforces this at
construction time -- nested overrides would invite infinite
recursion and, worse, make "which settings actually apply?"
undecidable without walking an unbounded tree.

Mode escalation across override levels
--------------------------------------

A project override's ``mode`` is applied as-is. This schema does
NOT enforce "override can only restrict, never escalate" -- that
is a policy the ``MemoryService`` can layer on top in P1.7 if
needed, based on real user feedback. The schema trusts the caller
that produced the settings: the settings are always written by an
authorized user (via the UI or the settings file), so the schema
does not need to second-guess them.

The rationale for keeping the schema permissive:

  * "Global RW, project X disabled" is the common case (sensitive
    project). It works.
  * "Global disabled, project X RW" is a maintenance-window case
    (memory off for most of the deployment, on for one project).
    It also works. Forbidding it would require awkward two-step
    edits.
  * Escalation surprises ("I disabled globally, why is project X
    still remembering?") are visible in the UI: the project's
    settings page shows its actual mode and highlights the
    divergence from the global default.

Design invariants
-----------------

1. **Pure domain, no I/O.**
   This module imports only stdlib + Pydantic + the sibling
   ``enums`` module. It never imports the repository, the
   settings manager, or any framework module.

2. **Complete defaults, round-trip safe.**
   ``model_dump(mode="json")`` produces a dict that
   ``MemorySettings.model_validate(...)`` accepts unchanged.
   ``MemorySettingsManager`` (P1.7) persists this dict directly to
   ``memory_settings.json``; loading is a single
   ``model_validate`` call. No custom (de)serialization logic.

3. **All limits enforced at construction.**
   Every numeric field has explicit ``ge`` / ``le`` bounds chosen
   to reject obvious misconfigurations (negative budgets, zero
   limits, warn thresholds outside ``[0, 1]``) while leaving room
   for legitimate extremes. A misconfigured settings file fails
   fast at startup, not silently at run time.

4. **Defaults are stable.**
   Every default below matches the plan's specification. Changing
   a default is a behavioral change that must be paired with an
   update to ``peldrun-memory-package`` format-version
   compatibility (P1.6), because imported packages were produced
   under the previous defaults.

5. **No mutable module-level singletons.**
   ``default_settings()`` returns a FRESH instance every call so
   that a caller who mutates the returned object cannot corrupt a
   shared state. The plan's "MemoryService singleton" pattern is
   preserved -- it is the SERVICE that is a singleton, not its
   configuration object.
"""

from __future__ import annotations

from typing import Dict, List

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from peldrun.memory.domain.enums import MemoryMode


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_MAX_PROJECT_OVERRIDES: int = 1_000
"""Hard ceiling on the number of project overrides in a single settings
instance.

A deployment with more than 1000 projects is either using the
override mechanism wrongly (overrides should be exceptional, not
per-project boilerplate) or has grown beyond what the settings file
is designed for. Capping here surfaces the situation early with a
clear error instead of allowing the JSON file to bloat to an
unmanageable size.
"""

_DEFAULT_PROVIDER_ID: str = "sqlite_fts"
"""The P1.3 provider that ships as the default backend."""


# ---------------------------------------------------------------------------
# MemoryTokenBudget
# ---------------------------------------------------------------------------


class MemoryTokenBudget(BaseModel):
    """Per-turn token allocation limits for the memory block.

    This budget is applied by ``MemoryContextBuilder`` (P1.4) when it
    assembles a ``MemoryContext`` from the ranked candidates. The
    total token count that reaches the LLM in the ``[RETRIEVED
    MEMORY]`` block is guaranteed to be ``<= max_tokens``, and the
    portion attributable to USER-scope entries is guaranteed to be
    ``<= reserved_for_user_scope`` (see the reservation rationale
    below).

    Reservation rationale
    ---------------------
    Without a reservation, a burst of high-scoring project-scope
    matches could starve user-scope entries out of the budget,
    silently hiding the user's stated preferences (e.g. "always use
    dark mode"). The ``reserved_for_user_scope`` field guarantees a
    slice of every turn's memory budget is available for user-scope
    entries, even if no user-scope match is above the relevance
    threshold in a given turn. The behavior of the reservation when
    unused is documented on ``MemoryContextBuilder`` in P1.4, not
    here.

    Warning threshold
    -----------------
    ``warn_threshold`` is the fraction of ``max_tokens`` at which
    the monitoring layer (P1.9) emits a "budget is getting tight"
    signal. It is a pure monitoring hint -- exceeding it does not
    drop entries or fail the run. It exists so that operators can
    see "the memory budget is 80% full on average" and raise
    ``max_tokens`` before it becomes a functional problem.
    """

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    max_tokens: int = Field(
        default=1024,
        ge=64,
        le=32_768,
        description=(
            "Absolute ceiling on tokens consumed by the memory block "
            "in a single turn. Must be at least 64 (a smaller cap "
            "cannot fit even one short entry plus the header) and "
            "at most 32768 (beyond that, memory is competing with "
            "the actual conversation for the context window)."
        ),
    )
    reserved_for_user_scope: int = Field(
        default=300,
        ge=0,
        description=(
            "Minimum tokens guaranteed to USER-scope entries within "
            "every turn's memory block. Zero disables the guarantee "
            "(user-scope entries compete with everyone else). Must "
            "not exceed ``max_tokens`` (enforced by the model "
            "validator)."
        ),
    )
    warn_threshold: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description=(
            "Fraction of ``max_tokens`` at which the monitoring "
            "layer flags the budget as 'tight'. 0.0 = warn always, "
            "1.0 = warn only at exactly 100% usage. Must be in "
            "[0.0, 1.0]."
        ),
    )

    @model_validator(mode="after")
    def _check_reservation_fits(self) -> "MemoryTokenBudget":
        """Enforce ``reserved_for_user_scope <= max_tokens``.

        A reservation larger than the total budget is meaningless
        (you cannot reserve 500 of a 300-token budget) and, more
        importantly, breaks the ``MemoryContext._check_budget``
        invariant downstream: the builder would be forced to
        consume more than ``max_tokens`` to honor the reservation.
        Rejecting the configuration at construction is the only
        safe response.
        """
        if self.reserved_for_user_scope > self.max_tokens:
            raise ValueError(
                f"reserved_for_user_scope ({self.reserved_for_user_scope}) "
                f"must not exceed max_tokens ({self.max_tokens}); a "
                f"reservation larger than the total budget cannot be "
                f"honored"
            )
        return self


# ---------------------------------------------------------------------------
# MemorySettings
# ---------------------------------------------------------------------------


class MemorySettings(BaseModel):
    """Complete configuration snapshot for the memory subsystem.

    See the module docstring for the relationship to ``RunRequest``,
    the ``project_overrides`` semantics, and the mode-escalation
    policy. The field-level documentation below focuses on HOW each
    knob is consumed by the surrounding layers, not on repeating
    the plan's high-level design.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        populate_by_name=True,
    )

    # ------------------------- global ---------------------------------------

    mode: MemoryMode = Field(
        default=MemoryMode.READ_WRITE,
        description=(
            "Global operating mode. DISABLED returns empty contexts "
            "on every retrieval and rejects every non-import write. "
            "READ_ONLY rejects writes EXCEPT ``IMPORT`` (the "
            "operator has explicitly confirmed an import via the "
            "preview → apply flow, which counts as user consent). "
            "READ_WRITE is the normal operating mode."
        ),
    )
    provider_id: str = Field(
        default=_DEFAULT_PROVIDER_ID,
        description=(
            "Identifier of the storage/retrieval backend provider "
            "(P1.3 registry). The default ``sqlite_fts`` ships with "
            "the platform. The registry validates the id at "
            "``MemoryService`` startup (P1.7); this field only "
            "requires a non-empty string."
        ),
    )

    # ------------------------- retrieval ------------------------------------

    default_limit: int = Field(
        default=5,
        ge=1,
        le=50,
        description=(
            "Maximum number of entries the ranker returns BEFORE "
            "budget fitting. The budget fitter (P1.4) may drop "
            "entries that exceed the token budget; this limit "
            "bounds the candidate set the fitter must consider. "
            "Typical values: 5 (default), 3 (memory-constrained "
            "deployments), 10-20 (memory-heavy applications)."
        ),
    )
    min_relevance_score: float = Field(
        default=0.3,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum NORMALIZED relevance score for an entry to be "
            "considered. Scores returned by the FTS backend are "
            "raw BM25 values; the ranker (P1.4) normalizes them to "
            "[0, 1] before comparing against this threshold. "
            "Raising the value reduces noise but can filter out "
            "legitimate matches; lowering it increases recall but "
            "risks injecting irrelevant memories."
        ),
    )
    boost_pinned: float = Field(
        default=1.5,
        ge=1.0,
        le=10.0,
        description=(
            "Multiplier applied to the score of entries with "
            "``is_pinned=True``. Values below 1.0 would be a "
            "DE-boost, which contradicts the field's name and is "
            "therefore rejected. Typical range: 1.2 (mild nudge) "
            "to 3.0 (pinned entries dominate). The default 1.5 "
            "matches the plan's specification."
        ),
    )

    # ------------------------- budget ---------------------------------------

    token_budget: MemoryTokenBudget = Field(
        default_factory=MemoryTokenBudget,
        description=(
            "Per-turn token allocation. See ``MemoryTokenBudget`` "
            "for the reservation and warning-threshold semantics."
        ),
    )

    # ------------------------- writing --------------------------------------

    auto_extraction_enabled: bool = Field(
        default=False,
        description=(
            "When True, the post-run outbox processor (P1.4/P1.7) "
            "extracts candidate memories from each completed run. "
            "Phase 1 defaults to False: extraction is opt-in and "
            "requires explicit enablement after the operator has "
            "validated the extraction quality on their data. The "
            "field is present in Phase 1 so the pipeline wiring can "
            "exist before the feature is turned on."
        ),
    )
    auto_extraction_min_turns: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Minimum number of completed conversation turns before "
            "the extractor (P1.4) considers running. Prevents the "
            "extractor from wasting CPU on trivial one-shot chats. "
            "Only consulted when ``auto_extraction_enabled`` is "
            "True."
        ),
    )

    # ------------------------- per-project overrides ------------------------

    project_overrides: Dict[str, "MemorySettings"] = Field(
        default_factory=dict,
        description=(
            "Per-project configuration deltas. Each entry maps a "
            "project id to a ``MemorySettings`` instance whose "
            "EXPLICITLY-SET fields override the corresponding "
            "global fields for that project. See "
            "``effective_for_project(project_id)`` for the merge "
            "semantics. Nested overrides (overrides that themselves "
            "carry ``project_overrides``) are rejected -- see "
            "``_reject_nested_overrides``."
        ),
    )

    # ------------------------- validators -----------------------------------

    @field_validator("provider_id", mode="after")
    @classmethod
    def _validate_provider_id(cls, v: str) -> str:
        """Trim and require non-empty."""
        if not isinstance(v, str):
            raise ValueError(
                f"provider_id must be a string, got {type(v).__name__}"
            )
        stripped = v.strip()
        if not stripped:
            raise ValueError("provider_id must not be empty")
        return stripped

    @model_validator(mode="after")
    def _reject_nested_overrides(self) -> "MemorySettings":
        """Reject ``project_overrides[*].project_overrides``.

        Nested overrides invite infinite recursion (an override can
        override an override, ad infinitum) and, more
        pragmatically, make "which settings actually apply to
        project X?" undecidable without walking an unbounded tree.
        The plan does not require nesting; the settings manager
        (P1.7) has no API path that would produce it. Enforcing the
        flat structure here turns any accidental producer into a
        fast, precise error instead of a silent deep-tree bug.
        """
        for project_id, override in self.project_overrides.items():
            if override.project_overrides:
                raise ValueError(
                    f"project_overrides[{project_id!r}] must not "
                    f"itself contain project_overrides; nested "
                    f"overrides are not supported. Edit the parent "
                    f"settings directly instead."
                )
        return self

    @field_validator("project_overrides", mode="after")
    @classmethod
    def _validate_override_count(
        cls, v: Dict[str, "MemorySettings"]
    ) -> Dict[str, "MemorySettings"]:
        """Cap the number of project overrides to prevent unbounded growth."""
        if len(v) > _MAX_PROJECT_OVERRIDES:
            raise ValueError(
                f"project_overrides exceeds the maximum of "
                f"{_MAX_PROJECT_OVERRIDES} entries "
                f"(got {len(v)}); consider using a smaller number "
                f"of shared project configuration templates"
            )
        return v

    # ------------------------- project resolution --------------------------

    def effective_for_project(self, project_id: str) -> "MemorySettings":
        """Resolve the effective settings for a specific project.

        Merge semantics
        ---------------
        If ``project_id`` has no override, this method returns
        ``self`` (fast path; no allocation, no field copying).

        If an override exists, the returned instance's field values
        are computed as follows, per field:

          * ``field in override.model_fields_set``
                → the override's value wins (the user
                  explicitly configured this field for the
                  project)
          * ``field not in override.model_fields_set``
                → the base (``self``) value is inherited

        This is Pydantic's standard "delta merge" semantics,
        exposed through ``model_dump(exclude_unset=True)``. It
        matches what a user means when they edit a single field on
        the project settings page: "change THIS field for THIS
        project, keep everything else from the defaults".

        Recursion prevention
        --------------------
        The returned instance always has ``project_overrides={}``.
        Even if the override somehow carried a nested
        ``project_overrides`` (which the validator rejects), this
        method would not propagate it. Belt-and-suspenders against
        a future relaxation of the validator.
        """
        if not isinstance(project_id, str):
            raise TypeError(
                f"project_id must be a string, got "
                f"{type(project_id).__name__}"
            )
        clean_pid = project_id.strip()
        if not clean_pid:
            # Empty project_id: no override can match. Return base.
            # Fail-fast was considered and rejected -- callers that
            # ask "what are the settings for project ''?" are often
            # in a fallback path and need a working settings object.
            return self

        override = self.project_overrides.get(clean_pid)
        if override is None:
            return self

        # Delta merge: base first, then explicitly-set override fields.
        base_data = self.model_dump(mode="json")
        override_delta = override.model_dump(
            mode="json",
            exclude_unset=True,
            exclude={"project_overrides"},
        )
        merged_data = {**base_data, **override_delta}
        # Force an empty overrides map to prevent any accidental
        # propagation of the original overrides into the resolved
        # instance.
        merged_data["project_overrides"] = {}
        return MemorySettings.model_validate(merged_data)

    # ------------------------- override editing ----------------------------

    def with_project_override(
        self,
        project_id: str,
        override: "MemorySettings",
    ) -> "MemorySettings":
        """Return a copy with ``project_id`` bound to ``override``.

        Convenience wrapper used by ``MemorySettingsManager`` (P1.7)
        when the operator edits a project's settings via the API.
        Returns a NEW ``MemorySettings`` instance; ``self`` is
        untouched. The new override REPLACES any previous one for
        the same project id.
        """
        clean_pid = self._clean_project_id(project_id)
        if override.project_overrides:
            raise ValueError(
                "override passed to with_project_override must not "
                "itself contain project_overrides; nested overrides "
                "are not supported"
            )
        new_overrides = dict(self.project_overrides)
        new_overrides[clean_pid] = override
        return self.model_copy(update={"project_overrides": new_overrides})

    def without_project_override(self, project_id: str) -> "MemorySettings":
        """Return a copy with ``project_id``'s override removed.

        Removes the override (reverting the project to global
        settings). Idempotent: removing a non-existent override is
        a no-op that still returns a valid new instance.
        """
        clean_pid = self._clean_project_id(project_id)
        if clean_pid not in self.project_overrides:
            return self
        new_overrides = dict(self.project_overrides)
        del new_overrides[clean_pid]
        return self.model_copy(update={"project_overrides": new_overrides})

    # ------------------------- internal helpers ----------------------------

    @staticmethod
    def _clean_project_id(project_id: str) -> str:
        """Trim and validate a project id for the override helpers."""
        if not isinstance(project_id, str):
            raise TypeError(
                f"project_id must be a string, got "
                f"{type(project_id).__name__}"
            )
        clean = project_id.strip()
        if not clean:
            raise ValueError("project_id must not be empty")
        return clean


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def default_settings() -> MemorySettings:
    """Return a fresh ``MemorySettings`` with plan-default values.

    Returns a NEW instance on every call. This is deliberate: the
    ``MemoryService`` singleton in P1.7 holds its OWN instance, and
    a caller that mutates a ``default_settings()`` result must not
    affect anyone else's view of "the defaults".

    Useful in tests, in the ``MemorySettingsManager.load()`` fallback
    path when no settings file exists, and in the API's "reset to
    defaults" endpoint (P1.7).
    """
    return MemorySettings()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


__all__ = [
    "MemorySettings",
    "MemoryTokenBudget",
    "default_settings",
]
