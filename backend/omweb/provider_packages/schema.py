# backend/omweb/provider_packages/schema.py
r"""
Typed schemas for the PELDRUN Provider Package subsystem.

Defines every Pydantic model used across the M9 administrative layer:

    * ``ProviderManifest``      — the on-disk ``manifest.json`` shape.
    * ``ProviderPackageEntry``  — one installed package (registry entry).
    * ``ProviderPackageRegistryIndex`` — the ``registry.json`` file.
    * ``ProviderPackageVersionRecord`` — one installed version.
    * ``ProviderPackageInstallResult`` — install response shape.
    * ``ProviderPackageHealthReport``  — health-check response shape.
    * ``ProviderPackageCatalogItem``   — one item in the store catalog.

Design invariants
-----------------

1. **Strict validation for the manifest, tolerant for internal state.**
   A malformed manifest is a security risk (permissions, entry point,
   dependencies). It is validated with ``extra="forbid"`` so a typo in a
   critical field is a hard error, not a silent no-op. Internal registry
   entries are read back from disk after a successful install, so they
   are more tolerant (``extra="allow"``) to allow future fields to be
   added without invalidating old data.

2. **Forbidden permissions fail at parse time.**
   ``shell.execute`` and ``workspace_access=true`` are rejected by
   Pydantic validators. There is no runtime that can enable them. The
   error message points the author to the correct alternative — provider
   adapters do not need filesystem access.

3. **Semver is enforced, not inferred.**
   ``version`` must match ``\d+\.\d+\.\d+``. The store cannot accept a
   version like ``"latest"`` or ``"1.0"`` — the rollback mechanism
   depends on version strings being ordered and unique.

4. **``api_contract`` is validated against the runtime's contract set.**
   A package declaring an unsupported contract is rejected during
   validation, not at request time. The user never sees a mysterious
   failure from a partially-loaded adapter.

5. **No optional fields where security depends on the value.**
   ``schema_version``, ``kind``, ``id``, ``name``, ``version``,
   ``api_contract``, and ``entry_point`` are all required. A manifest
   that omits one of them is a hard error.

See also
--------

    * ``backend/omweb/provider_packages/package_validator.py``
    * ``backend/omweb/provider_packages/package_manager.py``
    * ``backend/peldrun/llm/contracts.py`` — ``AdapterFeatures``
    * ``backend/peldrun/llm/providers/registry.py`` — ``ProviderRegistry``
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Set

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


#: The filename every Provider Package MUST contain at the archive root.
MANIFEST_FILENAME: str = "manifest.json"

#: The filename of the installed-package registry, placed in the root of
#: ``storage/store/providers/``.
REGISTRY_FILENAME: str = "registry.json"

#: The per-package file that records the currently active version.
#: Placed at ``storage/store/providers/<provider_id>/active.json``.
ACTIVE_POINTER_FILENAME: str = "active.json"

#: Per-version metadata file written inside each extracted version
#: directory. Unlike ``manifest.json`` (which comes from the archive),
#: this file is authored by PELDRUN and records the install-time facts
#: (hash, timestamp, source filename).
META_FILENAME: str = "_peldrun_meta.json"


#: The only supported package kind. Future kinds (e.g. ``peldrun.tool``)
#: would live in their own subsystems.
SUPPORTED_KIND: str = "peldrun.llm_provider"


#: Schema versions this build can parse. A package declaring a newer
#: schema is rejected with a clear error.
SUPPORTED_SCHEMA_VERSIONS: frozenset[str] = frozenset({"1.0.0"})


#: API contract versions this build can load. Must stay in sync with
#: ``peldrun.llm.providers.registry.SUPPORTED_API_CONTRACTS``.
SUPPORTED_API_CONTRACTS: frozenset[str] = frozenset(
    {"peldrun.llm.provider.v1"}
)


#: Archive size / count limits enforced by the validator.
MAX_PACKAGE_SIZE_BYTES: int = 100 * 1024 * 1024          # 100 MB
MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024          # 500 MB
MAX_FILE_COUNT: int = 2000
MAX_COMPRESSION_RATIO: int = 100                          # 100:1
MAX_PATH_LENGTH: int = 1024


#: Provider ID must be a filesystem-safe, URL-safe, snake-friendly
#: identifier. It is used as a directory name, a registry key, and an
#: entry-point name, so its character set is deliberately narrow.
_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


#: Semver pattern. The trailing portion is optional to accept versions
#: like ``"1.0.0-beta.1"`` and ``"1.0.0+build.42"``.
_VERSION_PATTERN = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


#: ``module.path:ClassName`` or ``module.path:function_name``.
_ENTRY_POINT_PATTERN = re.compile(
    r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$"
)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class ProviderTransport(str, Enum):
    """
    How the adapter talks to the provider.

    ``native``     — owns an SDK / REST client specific to the provider
                     (OpenAI, Anthropic, Google, Ollama, LM Studio).
    ``compatible`` — reuses an existing protocol (OpenAI-compatible,
                     Anthropic Messages-compatible) with only a manifest
                     file and endpoint configuration; NO code shipped.
    ``custom``     — a bespoke protocol that does not fit either shape;
                     requires the same code path as ``native`` but is
                     tagged differently for the UI.
    """

    NATIVE = "native"
    COMPATIBLE = "compatible"
    CUSTOM = "custom"


class PermissionKind(str, Enum):
    """
    Enumerable permissions a provider package may request.

    ``FORBIDDEN_PERMISSIONS`` (below) are rejected at parse time. The
    other permissions are surfaced to the user before activation.
    """

    #: The adapter may make HTTP requests to the connection's ``base_url``.
    NETWORK_CONNECT = "network.connect"

    #: The adapter may read its OWN connection's API key. It may NEVER
    #: read any other connection's secrets.
    SECRETS_READ_CONNECTION = "secrets.read:connection"

    #: The adapter may call the provider's model discovery endpoint.
    MODELS_DISCOVER = "models.discover"

    #: The adapter may send generation requests.
    LLM_GENERATE = "llm.generate"

    #: The adapter may open a streaming response.
    LLM_STREAM = "llm.stream"

    #: The adapter may read usage data for its own invocations.
    USAGE_READ = "usage.read"

    #: FORBIDDEN. Provider adapters never need workspace file access.
    WORKSPACE_READ = "workspace.read"

    #: FORBIDDEN. Provider adapters never need workspace file access.
    WORKSPACE_WRITE = "workspace.write"

    #: FORBIDDEN. Provider adapters are not allowed to spawn processes.
    SHELL_EXECUTE = "shell.execute"


#: Permissions that are unconditionally rejected. If a manifest declares
#: any of these, install fails with a clear error before any file lands
#: on disk.
FORBIDDEN_PERMISSIONS: frozenset[PermissionKind] = frozenset(
    {
        PermissionKind.WORKSPACE_READ,
        PermissionKind.WORKSPACE_WRITE,
        PermissionKind.SHELL_EXECUTE,
    }
)


#: Permissions that require explicit user acknowledgment during the
#: install confirmation dialog. The API returns these in the install
#: preview so the frontend can render a prominent warning.
SENSITIVE_PERMISSIONS: frozenset[PermissionKind] = frozenset(
    {
        PermissionKind.NETWORK_CONNECT,
        PermissionKind.SECRETS_READ_CONNECTION,
    }
)


# ---------------------------------------------------------------------------
# Nested manifest sub-models
# ---------------------------------------------------------------------------


class PackageRuntimeSpec(BaseModel):
    """
    The runtime requirements a provider package declares.

    Currently only ``python`` is supported. The ``python`` field carries
    a PEP 440 specifier (e.g. ``">=3.12,<3.13"``) that the dependency
    installer validates against the running interpreter before
    installing anything.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["python"] = "python"

    #: PEP 440 specifier. Optional — when omitted, the installer uses the
    #: interpreter's own version without checking.
    python: Optional[str] = None


class PackageFeatureFlags(BaseModel):
    """
    Feature flags a provider package declares.

    Every flag defaults to ``False``. A missing flag means "the adapter
    does not claim this feature" — the runtime will not attempt it.
    This is deliberately conservative: a package that omits
    ``tool_calling`` cannot accidentally have tools sent to it.

    The field set mirrors ``peldrun.llm.contracts.AdapterFeatures`` but
    is intentionally a subset. Fields not present here (e.g.
    ``prompt_caching``) are not surfaced in the manifest because they do
    not affect the install/activation contract.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    model_discovery: bool = False
    native_streaming: bool = False
    buffered_streaming: bool = False
    tool_calling: bool = False
    parallel_tool_calls: bool = False
    forced_tool_choice: bool = False
    vision: bool = False
    audio: bool = False
    structured_output: bool = False
    usage_reporting: bool = False
    reasoning: bool = False


class PackagePermissions(BaseModel):
    """
    The permissions a provider package requests.

    The shape mirrors the plan's manifest example::

        "permissions": {
          "network": "connection_endpoint",
          "secrets": ["connection.api_key"],
          "workspace_access": false
        }

    ``workspace_access=True`` and ``shell_execute=True`` are rejected by
    validators — the adapter cannot do either, and allowing the manifest
    to declare them would set a misleading precedent.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: Network scope. ``connection_endpoint`` is the only allowed value:
    #: the adapter may reach the connection's configured endpoint and
    #: nothing else. The Supervisor enforces this in phase 2 by
    #: restricting the adapter's network access to the resolved base URL.
    network: Literal["none", "connection_endpoint"] = "connection_endpoint"

    #: Secrets the adapter may read. Only ``"connection.api_key"`` is
    #: recognized. Any other value is rejected — the manifest cannot
    #: request access to arbitrary secrets.
    secrets: List[str] = Field(default_factory=list)

    #: Forbidden. Must remain ``false``.
    workspace_access: bool = False

    #: Forbidden. Must remain ``false``.
    shell_execute: bool = False

    @field_validator("secrets")
    @classmethod
    def _validate_secrets(cls, v: List[str]) -> List[str]:
        allowed = {"connection.api_key"}
        for entry in v:
            if not isinstance(entry, str) or entry not in allowed:
                raise ValueError(
                    f"Unsupported secret entry '{entry}'. Allowed values: "
                    f"{sorted(allowed)}. Provider adapters may only read "
                    f"the API key of their own connection."
                )
        return list(v)

    @field_validator("workspace_access")
    @classmethod
    def _reject_workspace_access(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "workspace_access must be false. Provider adapters never "
                "need filesystem access; they only communicate with the "
                "provider endpoint configured on the connection."
            )
        return v

    @field_validator("shell_execute")
    @classmethod
    def _reject_shell_execute(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "shell_execute must be false. Provider adapters are not "
                "permitted to spawn processes."
            )
        return v

    def to_permission_set(self) -> Set[PermissionKind]:
        """
        Convert the structured permission block into the flat
        ``PermissionKind`` set used by the enforcement layer.
        """
        result: Set[PermissionKind] = set()
        if self.network == "connection_endpoint":
            result.add(PermissionKind.NETWORK_CONNECT)
        if "connection.api_key" in self.secrets:
            result.add(PermissionKind.SECRETS_READ_CONNECTION)
        if self.workspace_access:
            result.add(PermissionKind.WORKSPACE_READ)
            result.add(PermissionKind.WORKSPACE_WRITE)
        if self.shell_execute:
            result.add(PermissionKind.SHELL_EXECUTE)
        return result


# ---------------------------------------------------------------------------
# Connection fields
# ---------------------------------------------------------------------------


class ConnectionFieldSpec(BaseModel):
    """
    One user-facing field in a provider's connection settings form.

    Each Provider Package declares its OWN connection fields in its
    ``manifest.json``. The Store's "Configure" modal reads these and
    renders the form dynamically — no changes to the frontend are
    required when a package adds new fields.

    Two fields are IMPLICIT and always present, even if the manifest
    does not declare them:

        * ``base_url`` — the connection's endpoint URL.
        * ``api_key``  — the connection's API key.

    Any extra fields declared here (e.g. Anthropic's ``anthropic_version``,
    Google Vertex's ``project_id``/``location``, Azure's
    ``deployment_name``/``api_version``) are:
        * rendered in the Store's Configure modal, in declaration order;
        * persisted to ``models_vault.json`` under the endpoint entry
          keyed by the package's provider id;
        * forwarded to the package's adapter via
          ``ConnectionContext.extra_params`` at request time, so the
          adapter's Python code can read them as it likes.

    The ``type`` drives the HTML input kind in the settings modal:
        * ``string``   → <input type="text">
        * ``password`` → <input type="password">
        * ``url``      → <input type="url">
        * ``number``   → <input type="number">
        * ``boolean``  → <input type="checkbox">
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str = Field(
        ...,
        description=(
            "The key persisted in models_vault.json for this endpoint. "
            "Must be a valid identifier (letters, digits, underscore)."
        ),
    )
    label: str = Field(
        ...,
        description="The user-facing label shown in the Configure modal.",
    )
    type: Literal[
        "string", "password", "url", "number", "boolean",
        "select", "textarea", "file", "secret_file",
    ] = "string"
    required: bool = False
    default: Optional[Any] = None
    placeholder: Optional[str] = None
    description: Optional[str] = Field(
        default=None,
        description=(
            "Optional helper text rendered under the input field."
        ),
    )

    # --- Select type ---
    #: For ``type == "select"``: the list of options, each a dict
    #: with ``value`` and ``label`` keys. Ignored for other types.
    options: Optional[List[Dict[str, str]]] = None

    # --- Conditional visibility ---
    #: A dict of ``{other_field_key: expected_value}``. When present,
    #: this field is only rendered when the referenced field(s) have
    #: the specified values. Example: ``{"scope": "B2B"}`` shows this
    #: field only when scope is B2B.
    visible_if: Optional[Dict[str, Any]] = None

    # --- File input ---
    #: For ``type`` in ``{"file", "secret_file"}``: a comma-separated
    #: accept list (e.g. ``".pem,.crt,.json"``). Ignored for other types.
    accept: Optional[str] = None

    # --- Validation ---
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    pattern: Optional[str] = Field(
        default=None,
        description=(
            "Optional regex the value must match. Client-side only — "
            "the adapter still validates on its own side."
        ),
    )


# ---------------------------------------------------------------------------
# Runtime hints
# ---------------------------------------------------------------------------


class RuntimeHints(BaseModel):
    """
    Non-functional hints a package can declare for the Store UI.

    These do NOT affect adapter behavior — they only drive:
        * warning banners in the Configure modal;
        * documentation links;
        * custom header injection at request build time
          (``custom_headers_from_fields``).

    The adapter code (shipped inside the package) is the single source
    of truth for actually ENFORCING these — the hints only alert the
    user before they save a connection.

    Example (GigaChat)::

        "runtime_hints": {
          "requires_custom_tls": true,
          "requires_token_refresh": true,
          "docs_url": "https://developers.sber.ru/docs/ru/gigachat"
        }

    Example (OpenRouter)::

        "runtime_hints": {
          "custom_headers_from_fields": {
            "http_referer": "HTTP-Referer",
            "x_title": "X-Title"
          }
        }
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    #: The provider requires a custom TLS / CA bundle that certifi
    #: does not include (e.g. Russian Root CA for GigaChat).
    requires_custom_tls: bool = False

    #: The provider requires mutual TLS (client certificate).
    requires_mtls: bool = False

    #: Access tokens expire and must be refreshed manually. The UI
    #: shows a note and, if ``token_lifetime_seconds`` is present,
    #: a countdown warning.
    requires_token_refresh: bool = False
    token_lifetime_seconds: Optional[int] = None

    #: Whether the adapter reads from environment variables when a
    #: connection field is empty. Useful for CI / Docker deployments.
    supports_env_vars: bool = False

    #: A link to the provider's documentation. Rendered as a small
    #: "Docs" link in the Configure modal.
    docs_url: Optional[str] = None

    #: Maps a ``connection_field.key`` to an HTTP header name. When a
    #: request is built, the runtime copies the field's value into the
    #: outgoing header. Used by providers that require custom headers
    #: (OpenRouter's HTTP-Referer / X-Title, for example).
    custom_headers_from_fields: Dict[str, str] = Field(
        default_factory=dict
    )


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------


class ProviderManifest(BaseModel):
    """
    The validated on-disk ``manifest.json`` shape for a Provider Package.

    Construction rules
    ------------------
    * ``schema_version``, ``kind``, ``id``, ``name``, ``version``,
      ``api_contract``, and ``entry_point`` are REQUIRED. A manifest
      missing any of them is rejected before any file touches disk.
    * ``extra="forbid"``: a typo in a field name (``dependecies`` instead
      of ``dependencies``) is a hard error. Silent dropping of
      security-critical fields is not acceptable.
    * Validators enforce the semver pattern, the provider ID pattern, the
      supported contract version, and the supported schema version.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # --- Identity (all required) ---
    schema_version: str
    kind: Literal["peldrun.llm_provider"]
    id: str
    name: str
    version: str

    # --- Contract (all required) ---
    api_contract: str

    #: For ``native`` / ``custom`` transports, the ``module.path:ClassName``
    #: entry point of the adapter implementation. Required for those
    #: transports; must be ``None`` for ``compatible``.
    entry_point: Optional[str] = None

    #: For ``compatible`` transports, the built-in adapter to reuse
    #: (e.g. ``"openai_compat"``). The plugin host resolves it via the
    #: built-in registry. Must be ``None`` for ``native`` / ``custom``.
    base_adapter: Optional[str] = None

    # --- Optional identity ---
    description: str = ""
    display_name: Optional[str] = None
    author: Optional[str] = None
    author_url: Optional[str] = None
    homepage: Optional[str] = None
    license: Optional[str] = None
    icon: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    # --- Connection fields (per-package form) ---
    #: The fields the user fills in when connecting this provider.
    #:
    #: Empty list → the Store's Configure modal shows the default two
    #: fields only (``base_url``, ``api_key``), which covers ~95% of
    #: real-world providers.
    #:
    #: Non-empty → the modal shows those two PLUS every field declared
    #: here, in order. The package's adapter receives the extra values
    #: via ``ConnectionContext.extra_params`` — so its Python code can
    #: use them however it needs.
    connection_fields: List[ConnectionFieldSpec] = Field(
        default_factory=list
    )

    #: Non-functional hints for the Store UI (TLS warnings, docs link,
    #: custom headers). Do NOT affect adapter behavior — the adapter
    #: code shipped inside the package is the single source of truth.
    runtime_hints: RuntimeHints = Field(default_factory=RuntimeHints)

    # --- Runtime ---
    runtime: PackageRuntimeSpec = Field(default_factory=PackageRuntimeSpec)
    transport: ProviderTransport = ProviderTransport.NATIVE

    #: Python package dependencies. Installed into the package's isolated
    #: venv, NOT the PELDRUN main environment.
    dependencies: List[str] = Field(default_factory=list)

    # --- Capabilities and permissions ---
    features: PackageFeatureFlags = Field(default_factory=PackageFeatureFlags)
    permissions: PackagePermissions = Field(default_factory=PackagePermissions)

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, v: str) -> str:
        if v not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(
                f"schema_version '{v}' is not supported. "
                f"Supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}."
            )
        return v

    @field_validator("api_contract")
    @classmethod
    def _validate_api_contract(cls, v: str) -> str:
        if v not in SUPPORTED_API_CONTRACTS:
            raise ValueError(
                f"api_contract '{v}' is not supported. "
                f"Supported: {sorted(SUPPORTED_API_CONTRACTS)}."
            )
        return v

    @field_validator("id")
    @classmethod
    def _validate_id(cls, v: str) -> str:
        if not v or not isinstance(v, str):
            raise ValueError("provider 'id' must be a non-empty string.")
        if not _PROVIDER_ID_PATTERN.match(v):
            raise ValueError(
                f"provider 'id' '{v}' must match "
                f"{_PROVIDER_ID_PATTERN.pattern} (lowercase letters, "
                f"digits, dot, dash, underscore; must start with a "
                f"letter or digit)."
            )
        return v

    @field_validator("version")
    @classmethod
    def _validate_version(cls, v: str) -> str:
        if not v or not _VERSION_PATTERN.match(v):
            raise ValueError(
                f"version '{v}' must be semver "
                f"(e.g. '1.0.0', '1.0.0-beta.1')."
            )
        return v

    @field_validator("entry_point")
    @classmethod
    def _validate_entry_point(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not _ENTRY_POINT_PATTERN.match(v):
            raise ValueError(
                f"entry_point '{v}' must be 'module.path:ClassName' "
                f"(e.g. 'acme_peldrun.adapter:AcmeProvider')."
            )
        return v

    @field_validator("base_adapter")
    @classmethod
    def _validate_base_adapter(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "base_adapter must be a non-empty string when provided."
            )
        if not _PROVIDER_ID_PATTERN.match(v):
            raise ValueError(
                f"base_adapter '{v}' must match "
                f"{_PROVIDER_ID_PATTERN.pattern}."
            )
        return v

    @model_validator(mode="after")
    def _validate_transport_contract(self) -> "ProviderManifest":
        """
        Enforce cross-field consistency between ``transport``,
        ``entry_point``, and ``base_adapter``.

        Rules
        -----
        * ``compatible`` transport:
            - MUST declare ``base_adapter``.
            - MUST NOT declare ``entry_point`` (no code is shipped).
        * ``native`` / ``custom`` transports:
            - MUST declare ``entry_point``.
            - MUST NOT declare ``base_adapter`` (that field is for
              reusing an existing adapter, not for shipping code).
        """
        if self.transport == ProviderTransport.COMPATIBLE:
            if not self.base_adapter:
                raise ValueError(
                    "Compatible packages must declare 'base_adapter' "
                    "(the built-in adapter to reuse, e.g. "
                    "'openai_compat')."
                )
            if self.entry_point:
                raise ValueError(
                    "Compatible packages must NOT declare 'entry_point' "
                    "— they ship no code and reuse an existing adapter."
                )
        else:
            if not self.entry_point:
                raise ValueError(
                    f"Transport '{self.transport.value}' requires "
                    f"'entry_point' (module:ClassName)."
                )
            if self.base_adapter:
                raise ValueError(
                    f"Transport '{self.transport.value}' must NOT "
                    f"declare 'base_adapter' — that field is reserved "
                    f"for 'compatible' transports."
                )
        return self

    @field_validator("dependencies")
    @classmethod
    def _validate_dependencies(cls, v: List[str]) -> List[str]:
        # An empty string in the dependency list would confuse pip.
        for entry in v:
            if not isinstance(entry, str) or not entry.strip():
                raise ValueError(
                    "Every entry in 'dependencies' must be a non-empty "
                    "PEP 508 specifier string."
                )
        return list(v)


# ---------------------------------------------------------------------------
# Installed-package records
# ---------------------------------------------------------------------------


class ProviderPackageVersionRecord(BaseModel):
    """
    One installed version of a provider package.

    The hash is the SHA-256 of the original uploaded ZIP, computed at
    install time. It is used as the version's storage directory name and
    as the idempotency key for re-installs of the same archive.

    ``entry_point`` and ``class_name`` are ``Optional[str]``: they are
    set for ``native`` / ``custom`` transports and left ``None`` for
    ``compatible`` transports, which ship no Python code and reuse a
    built-in adapter via ``base_adapter``.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    version: str
    hash: str
    installed_at: str
    size_bytes: int
    file_count: int
    entry_point: Optional[str] = None
    class_name: Optional[str] = None


class ProviderPackageEntry(BaseModel):
    """
    One installed provider package.

    This is what ``registry.json`` stores per provider, and what the API
    returns in ``GET /api/store/providers``. It carries everything the
    UI needs to render a card (name, description, status, permissions,
    features) without reading the manifest again.

    The ``active_version`` and ``active_hash`` are the runtime's selected
    version. They are kept on the entry (not only in ``active.json``) so
    that a registry read gives a consistent snapshot without a second
    disk read.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    display_name: Optional[str] = None
    description: str = ""

    active_version: str
    active_hash: str

    #: Runtime toggle. Disabled packages remain registered but cannot
    #: be instantiated by the factory.
    enabled: bool = True

    installed_versions: List[ProviderPackageVersionRecord] = Field(
        default_factory=list
    )

    #: Mirrored from the manifest for easy UI rendering.
    transport: str = ProviderTransport.NATIVE.value
    api_contract: str = "peldrun.llm.provider.v1"
    features: Dict[str, bool] = Field(default_factory=dict)
    permissions: Dict[str, Any] = Field(default_factory=dict)
    tags: List[str] = Field(default_factory=list)
    author: Optional[str] = None
    homepage: Optional[str] = None

    #: How this package arrived: ``"uploaded"`` (user ZIP) or
    #: ``"catalog"`` (installed from the store catalog).
    source: str = "uploaded"

    #: Optional author website (populated from the manifest).
    author_url: Optional[str] = None

    #: The manifest's ``connection_fields``, snapshotted at install
    #: time. Stored as plain dicts so schema evolution does not
    #: require a registry migration — the manifest remains the source
    #: of truth for strict validation; the entry is a tolerant
    #: snapshot for the UI.
    connection_fields: List[Dict[str, Any]] = Field(default_factory=list)

    #: The manifest's ``runtime_hints``, snapshotted at install time.
    #: Same rationale as above — plain dict for forward compatibility.
    runtime_hints: Dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # Convenience
    # ------------------------------------------------------------------

    def get_version(
        self, version: str
    ) -> Optional[ProviderPackageVersionRecord]:
        """Return the record for ``version``, or ``None`` if not installed."""
        for record in self.installed_versions:
            if record.version == version:
                return record
        return None

    def has_version(self, version: str) -> bool:
        return self.get_version(version) is not None


class ProviderPackageRegistryIndex(BaseModel):
    """
    The ``registry.json`` file contents.

    ``schema_version`` is bumped if the on-disk shape changes in a way
    that requires discarding older data. ``packages`` is keyed by the
    provider ID (the canonical manifest ``id`` field).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = 1
    updated_at: str = ""
    packages: Dict[str, ProviderPackageEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Install result
# ---------------------------------------------------------------------------


class ProviderPackageStepReport(BaseModel):
    """
    Report for a single step of the install pipeline.

    Steps are named (``"validate"``, ``"extract"``, ``"manifest"``,
    ``"venv_create"``, ``"pip_install"``, ``"register"``) so the UI can
    display a progress timeline without parsing free-text messages.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    status: Literal["ok", "failed", "skipped"] = "ok"
    duration_ms: int = 0
    detail: str = ""


class ProviderPackageInstallReport(BaseModel):
    """
    Full report produced by a successful install.

    ``venv_path`` is the absolute path to the isolated Python virtual
    environment created for this version. It is ``None`` for
    ``compatible`` packages that ship no Python code (manifest-only).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    dependencies_installed: bool = False
    dependencies_skipped: bool = True
    venv_path: Optional[str] = None
    duration_ms: int = 0
    steps: List[ProviderPackageStepReport] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class ProviderPackageInstallResult(BaseModel):
    """
    Response shape returned by ``POST /api/store/providers/upload``.

    Mirrors the Tool Bundle install result shape so that the frontend
    upload flow can reuse the same UI logic with minimal changes.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    version: str
    hash: str
    name: str
    display_name: Optional[str] = None
    description: str = ""
    size_bytes: int
    file_count: int
    transport: str = ProviderTransport.NATIVE.value
    features: Dict[str, bool] = Field(default_factory=dict)
    permissions: Dict[str, Any] = Field(default_factory=dict)
    install_report: ProviderPackageInstallReport = Field(
        default_factory=ProviderPackageInstallReport
    )
    warnings: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class ProviderPackageHealthCheck(BaseModel):
    """One filesystem or runtime check performed by the health endpoint."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    name: str
    passed: bool
    message: str = ""


class ProviderPackageHealthReport(BaseModel):
    """
    Response shape returned by
    ``GET /api/store/providers/{id}/health``.

    ``status`` is:
        * ``healthy``  — every check passed.
        * ``degraded`` — some checks failed but the package is loadable.
        * ``broken``   — no meaningful check passed.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    version: str
    hash: str
    status: Literal["healthy", "degraded", "broken"]
    checks: List[ProviderPackageHealthCheck] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class ProviderPackageCatalogItem(BaseModel):
    """
    One entry in the store catalog (``GET /api/store/providers/catalog``).

    The catalog is a static index (bundled with PELDRUN or fetched from
    a PELDRUN-operated endpoint in the future). Each item describes a
    package a user can install with one click. When ``download_url`` is
    present, the install path downloads the ZIP and runs the same
    validation pipeline as a manual upload.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    id: str
    name: str
    display_name: Optional[str] = None
    description: str = ""
    version: str
    transport: str = ProviderTransport.NATIVE.value
    features: Dict[str, bool] = Field(default_factory=dict)
    permissions: Dict[str, Any] = Field(default_factory=dict)

    #: When present, ``POST /api/store/providers/{id}/install-from-catalog``
    #: downloads and validates this URL. When ``None``, the item is
    #: informational only (a preview of an upcoming package).
    download_url: Optional[str] = None

    #: Optional SHA-256 hex digest of the ZIP. When present, the download
    #: path verifies the hash before extraction.
    checksum_sha256: Optional[str] = None

    author: Optional[str] = None
    author_url: Optional[str] = None
    homepage: Optional[str] = None
    tags: List[str] = Field(default_factory=list)

    #: Connection fields the package's manifest declares. Empty list
    #: means "use defaults (base_url + api_key)". For preview-only
    #: catalog items this is still useful: the UI can show the user
    #: what setup will be needed BEFORE they install.
    connection_fields: List["ConnectionFieldSpec"] = Field(
        default_factory=list
    )


__all__ = [
    # Constants
    "MANIFEST_FILENAME",
    "REGISTRY_FILENAME",
    "ACTIVE_POINTER_FILENAME",
    "META_FILENAME",
    "SUPPORTED_KIND",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SUPPORTED_API_CONTRACTS",
    "MAX_PACKAGE_SIZE_BYTES",
    "MAX_UNCOMPRESSED_BYTES",
    "MAX_FILE_COUNT",
    "MAX_COMPRESSION_RATIO",
    "MAX_PATH_LENGTH",
    # Enums
    "ProviderTransport",
    "PermissionKind",
    "FORBIDDEN_PERMISSIONS",
    "SENSITIVE_PERMISSIONS",
    # Nested manifest
    "PackageRuntimeSpec",
    "PackageFeatureFlags",
    "PackagePermissions",
    "ConnectionFieldSpec",
    "RuntimeHints",
    # Manifest
    "ProviderManifest",
    # Installed state
    "ProviderPackageVersionRecord",
    "ProviderPackageEntry",
    "ProviderPackageRegistryIndex",
    # Results
    "ProviderPackageStepReport",
    "ProviderPackageInstallReport",
    "ProviderPackageInstallResult",
    "ProviderPackageHealthCheck",
    "ProviderPackageHealthReport",
    "ProviderPackageCatalogItem",
]
