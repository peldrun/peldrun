# backend/omweb/provider_packages/permissions.py
"""
Permission model and enforcement for Provider Packages.

A Provider Package declares its permissions in ``manifest.json`` (see
``schema.PackagePermissions``). This module converts that declaration
into a flat ``frozenset[PermissionKind]`` and exposes helpers the
Supervisor (phase 2) will use to enforce them at adapter-call time.

Why a separate module from ``schema.py``
----------------------------------------

``schema.py`` defines the DECLARATION shape — what a manifest may
request. This module defines the ENFORCEMENT surface — what the runtime
is allowed to do with that declaration. The two are separated because:

    * The schema must remain stable forever (renaming a field breaks
      existing packages).
    * The enforcement layer will evolve as the Supervisor grows new
      checks. Keeping enforcement out of schema lets us add checks
      without touching the manifest contract.

Design invariants
-----------------

1. **Forbidden permissions never appear in an effective set.**
   ``PermissionKind.WORKSPACE_READ``, ``WORKSPACE_WRITE``, and
   ``SHELL_EXECUTE`` are rejected by ``PackagePermissions`` validators
   at parse time. This module re-validates because defense in depth is
   cheap and this is a security boundary.

2. **``network: "none"`` grants no network access.**
   A manifest that explicitly disables network access is honored — the
   Supervisor must not open a socket on its behalf even if some other
   permission implies it. This is the only "subtractive" permission in
   the model.

3. **Sensitive permissions must be surfaced to the user.**
   ``NETWORK_CONNECT`` and ``SECRETS_READ_CONNECTION`` are flagged via
   ``sensitive_permission_values()`` so the install UI can present them
   prominently. A future "require acknowledgment" flag will hook into
   this list.

4. **``PermissionSet`` is immutable.**
   Once constructed, an effective permission set cannot be mutated.
   This prevents a downstream bug from adding permissions a manifest
   never granted.

5. **The model has no "root".** There is no catch-all permission. Every
   action a package can take is one of the enumerated kinds. This keeps
   auditing tractable.

See also
--------

    * ``backend/omweb/provider_packages/schema.py`` — declaration shape
    * ``backend/omweb/provider_packages/errors.py`` — exception types
    * ``backend/peldrun/llm/plugin_host/permissions.py`` — runtime enforcer
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Set

from omweb.provider_packages.errors import (
    ProviderPackagePermissionError,
    ProviderPackageValidationError,
)
from omweb.provider_packages.schema import (
    FORBIDDEN_PERMISSIONS,
    SENSITIVE_PERMISSIONS,
    PackagePermissions,
    PermissionKind,
)

logger = logging.getLogger("omweb.provider_packages.permissions")


# ---------------------------------------------------------------------------
# Immutable permission set
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PermissionSet:
    """
    The effective permissions granted to a Provider Package.

    Construct via ``from_manifest_permissions()`` — the constructor
    itself accepts any ``frozenset[PermissionKind]`` but callers should
    not build one directly, because the module-level helper applies
    the forbidden-permission check that the constructor cannot express
    as a type.

    The set is intentionally narrow: a provider adapter needs to read
    its own connection's API key, hit its endpoint, and send/receive
    LLM traffic. Nothing else.
    """

    granted: frozenset[PermissionKind]

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def has(self, kind: PermissionKind) -> bool:
        """Return True iff ``kind`` is granted."""
        return kind in self.granted

    def require(self, kind: PermissionKind) -> None:
        """
        Raise ``ProviderPackagePermissionError`` if ``kind`` is not
        granted.

        Callers use this at the point of use (e.g. before opening a
        network connection). Raising at the point of use is more
        reliable than raising at construction time — a package that
        never reaches the code path that needs ``network.connect``
        does not pay for declaring it.
        """
        if kind not in self.granted:
            raise ProviderPackagePermissionError(
                f"Permission '{kind.value}' has not been granted to this "
                f"provider package. Declare it in the manifest's "
                f"'permissions' block to enable the corresponding "
                f"capability.",
                details={
                    "requested": kind.value,
                    "granted": sorted(p.value for p in self.granted),
                },
            )

    def is_empty(self) -> bool:
        """Return True iff no permissions are granted."""
        return not self.granted

    def sensitive(self) -> Set[PermissionKind]:
        """Return the subset of granted permissions that require user attention."""
        return {p for p in self.granted if p in SENSITIVE_PERMISSIONS}

    def to_sorted_values(self) -> list:
        """Return the granted permissions as a sorted list of strings."""
        return sorted(p.value for p in self.granted)

    def __contains__(self, item: object) -> bool:
        """Allow ``PermissionKind.NETWORK_CONNECT in perm_set``."""
        if isinstance(item, PermissionKind):
            return item in self.granted
        return False

    def __iter__(self):
        return iter(self.granted)

    def __len__(self) -> int:
        return len(self.granted)


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def from_manifest_permissions(
    declared: PackagePermissions,
    *,
    granted_extras: Iterable[PermissionKind] = (),
) -> PermissionSet:
    """
    Build the effective ``PermissionSet`` from a manifest declaration.

    Parameters
    ----------
    declared
        The ``PackagePermissions`` block parsed from the manifest.
        Constructing a ``PackagePermissions`` already rejects forbidden
        permissions, so this function usually succeeds.
    granted_extras
        Optional explicit grants applied by the installer. Reserved for
        a future admin-only flow (e.g. "grant shell access to this
        package for debugging"). Today it MUST be empty; the function
        raises ``ProviderPackageValidationError`` if any of the
        forbidden permissions appear here.

    Raises
    ------
    ProviderPackageValidationError
        When a forbidden permission appears in either ``declared`` or
        ``granted_extras``. This is a redundant check — it exists
        specifically so that future changes to ``PackagePermissions``
        cannot accidentally open a hole.
    """
    # ------------------------------------------------------------------
    # 1. Convert the declared block into a flat set.
    # ------------------------------------------------------------------
    effective: Set[PermissionKind] = set()

    if declared.network == "connection_endpoint":
        effective.add(PermissionKind.NETWORK_CONNECT)

    if "connection.api_key" in (declared.secrets or []):
        effective.add(PermissionKind.SECRETS_READ_CONNECTION)

    # Note: ``workspace_access`` and ``shell_execute`` are rejected by
    # the PackagePermissions validators, so these branches should never
    # fire. They exist as defense-in-depth against future changes.
    if declared.workspace_access:
        effective.add(PermissionKind.WORKSPACE_READ)
        effective.add(PermissionKind.WORKSPACE_WRITE)
    if declared.shell_execute:
        effective.add(PermissionKind.SHELL_EXECUTE)

    # ------------------------------------------------------------------
    # 2. Add extras (empty today; reserved for admin flows).
    # ------------------------------------------------------------------
    for extra in granted_extras:
        if not isinstance(extra, PermissionKind):
            raise ProviderPackageValidationError(
                f"granted_extras must contain PermissionKind values; "
                f"got {type(extra).__name__}.",
            )
        effective.add(extra)

    # ------------------------------------------------------------------
    # 3. Forbidden-permission guard (defense in depth).
    # ------------------------------------------------------------------
    forbidden = effective & FORBIDDEN_PERMISSIONS
    if forbidden:
        raise ProviderPackageValidationError(
            f"Provider packages cannot hold forbidden permissions: "
            f"{sorted(p.value for p in forbidden)}. These are reserved "
            f"for first-party tooling and never granted to Provider "
            f"Packages.",
            details={
                "forbidden": sorted(p.value for p in forbidden),
            },
        )

    return PermissionSet(granted=frozenset(effective))


def from_values(values: Iterable[str]) -> PermissionSet:
    """
    Build a ``PermissionSet`` from a list of permission string values.

    Used when reading a persisted registry entry back into memory. The
    forbidden-permission guard is still applied — a corrupted registry
    file cannot smuggle in a permission the schema forbids.

    Unknown strings raise ``ProviderPackageValidationError`` with a
    clear message listing the recognized values.
    """
    effective: Set[PermissionKind] = set()
    for raw in values:
        try:
            effective.add(PermissionKind(raw))
        except ValueError:
            raise ProviderPackageValidationError(
                f"Unknown permission value {raw!r}. Recognized: "
                f"{sorted(p.value for p in PermissionKind)}.",
                details={"unknown": raw},
            )

    forbidden = effective & FORBIDDEN_PERMISSIONS
    if forbidden:
        raise ProviderPackageValidationError(
            f"Persisted permission set contains forbidden values: "
            f"{sorted(p.value for p in forbidden)}.",
            details={
                "forbidden": sorted(p.value for p in forbidden),
            },
        )

    return PermissionSet(granted=frozenset(effective))


# ---------------------------------------------------------------------------
# Convenience re-exports for UI
# ---------------------------------------------------------------------------


def sensitive_permission_values(
    perm_set: PermissionSet,
) -> list:
    """
    Return the human-readable warning strings for the sensitive
    permissions held by ``perm_set``.

    The strings are designed to be displayed verbatim in the install
    confirmation dialog. They are intentionally concrete — "may make
    HTTP requests to the configured endpoint" beats "needs network".
    """
    warnings: list = []
    if perm_set.has(PermissionKind.NETWORK_CONNECT):
        warnings.append(
            "May make outbound HTTP requests to the connection's "
            "configured endpoint. It cannot reach other hosts."
        )
    if perm_set.has(PermissionKind.SECRETS_READ_CONNECTION):
        warnings.append(
            "May read the API key for THIS connection only. It cannot "
            "access other connections' credentials."
        )
    return warnings


def describe_permission(kind: PermissionKind) -> str:
    """
    Return a one-line human description of a permission.

    Used by the install preview and the details drawer. Kept here (not
    in the schema) so that future enforcement modules can extend the
    descriptions without touching the schema.
    """
    mapping = {
        PermissionKind.NETWORK_CONNECT: (
            "Open outbound HTTP connections to the configured endpoint."
        ),
        PermissionKind.SECRETS_READ_CONNECTION: (
            "Read the API key of its own connection."
        ),
        PermissionKind.MODELS_DISCOVER: (
            "Query the provider's model discovery endpoint."
        ),
        PermissionKind.LLM_GENERATE: (
            "Send LLM generation requests."
        ),
        PermissionKind.LLM_STREAM: (
            "Open streaming responses from the provider."
        ),
        PermissionKind.USAGE_READ: (
            "Read usage data for its own invocations."
        ),
        PermissionKind.WORKSPACE_READ: (
            "FORBIDDEN — Provider Packages cannot read workspace files."
        ),
        PermissionKind.WORKSPACE_WRITE: (
            "FORBIDDEN — Provider Packages cannot write workspace files."
        ),
        PermissionKind.SHELL_EXECUTE: (
            "FORBIDDEN — Provider Packages cannot spawn processes."
        ),
    }
    return mapping.get(kind, kind.value)


__all__ = [
    "PermissionSet",
    "from_manifest_permissions",
    "from_values",
    "sensitive_permission_values",
    "describe_permission",
]
