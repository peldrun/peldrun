# PELDRUN Provider Packages — Developer Documentation
---

### `docs/providers/README.md`

```markdown
<!-- docs/providers/README.md -->
# PELDRUN Provider Packages — Developer Documentation

> **Status**: Stable. This documentation describes the system implemented in
> `backend/omweb/provider_packages/` and `backend/peldrun/llm/plugin_host/`.

---

## Table of Contents

- [What are Provider Packages?](#what-are-provider-packages)
- [Who this documentation is for](#who-this-documentation-is-for)
- [Key concepts](#key-concepts)
- [Reading paths](#reading-paths)
- [Documentation index](#documentation-index)
- [30-second quickstart](#30-second-quickstart)
- [Support](#support)

---

## What are Provider Packages?

A **Provider Package** is an installable bundle that teaches PELDRUN how to
communicate with a new LLM provider — without requiring any changes to the
PELDRUN codebase itself. Users install packages from the **Store → Providers**
section of the UI. Each package is self-contained and manages its own:

- Connection form (which fields the user needs to fill in)
- Runtime transport (how requests are sent to the provider)
- Credentials handling (which fields are secrets)
- Optional custom code (a Python adapter that speaks the provider's wire
  protocol)

### The problem they solve

Before Provider Packages existed, adding a new LLM provider required:

1. Editing `backend/peldrun/llm/providers/__init__.py` to register the adapter
2. Editing `backend/peldrun/llm/providers/factory.py` to instantiate it
3. Editing `backend/omweb/routers/config_rtr.py` to handle its discovery
4. Editing the frontend LLM Settings UI to add a card
5. Editing `dispatcher.py` to route to it
6. Shipping a new PELDRUN release

That's **six coordinated changes** for every provider. With Provider Packages,
adding a new provider is **one package** — no core code changes, no release
required. The user installs it in seconds.

### Three transport types

Every Provider Package declares a **transport** that determines how it talks
to the LLM:

| Transport | Ships code? | Use case | Effort to create |
|-----------|-------------|----------|------------------|
| **`compatible`** | ❌ No | Provider speaks a protocol that PELDRUN already supports (OpenAI Chat Completions, Anthropic Messages, Ollama Native, LM Studio Native). | ~5 minutes |
| **`native`** | ✅ Yes | Provider has its own API / SDK that no existing adapter speaks. | ~1-2 hours |
| **`custom`** | ✅ Yes | Provider's protocol is unique and does not fit any standard shape. | ~2-4 hours |

**Most providers fit `compatible`.** Of the 20+ major LLM providers in early
2025, roughly 15 expose an OpenAI-compatible endpoint and can be shipped as a
`compatible` package with zero code. The remaining 5 (Anthropic, Google Vertex,
AWS Bedrock, GigaChat, Yandex) require `native` packages because they use
proprietary auth schemes or wire formats.

See [Manifest Reference](04-manifest-reference.md#transport) for the full
transport decision tree.

---

## Who this documentation is for

### Audience 1: Application developers

You want to **add support for a new LLM provider** to PELDRUN — either because
the provider is not yet in the store catalog, or because you want to ship a
customized version.

**Start here:** [Quickstart — Compatible](02-quickstart-compatible.md) if the
provider is OpenAI-compatible. [Quickstart — Native](03-quickstart-native.md)
otherwise.

### Audience 2: End users

You installed a Provider Package and need help setting it up, or you want to
understand what permissions it asks for.

**Start here:** [Connection Management](10-connection-management.md) and
[Permissions](08-permissions.md).

### Audience 3: Contributors and maintainers

You want to understand the full architecture, extend the system, or audit the
security model.

**Start here:** [Architecture](01-architecture.md), then
[Security Model](12-security-model.md).

---

## Key concepts

Here is a mental model of the whole system. Read this section once; the
detailed docs assume you know these concepts.

### Package

A single installable artifact. It is a ZIP file containing at minimum a
`manifest.json` — and for `native` / `custom` transports, also Python code and
a `requirements.txt`.

**Lives at:** `<backend>/storage/store/providers/<provider_id>/versions/<version>/`

### Manifest

The `manifest.json` inside a package. It declares:

- Identity (id, name, version, author)
- Transport (`native` / `compatible` / `custom`)
- Connection fields (what the user must fill in)
- Runtime hints (warnings for the UI)
- Permissions (what the adapter is allowed to do)
- Feature flags (streaming, tools, vision, reasoning, ...)

**Full schema:** [Manifest Reference](04-manifest-reference.md).

### Adapter

The Python class (for `native` / `custom` transports only) that translates
PELDRUN's `LLMRequest` into the provider's wire format and parses the response
back. It runs inside an isolated worker subprocess managed by the
**plugin_host**.

**Contract:** [Adapter Protocol](06-adapter-protocol.md).

### Connection

One saved set of credentials for a package. A single package can have
**multiple connections** — for example, "Personal Account", "Work Team", and
"Sandbox" — with exactly one marked **active** at any time. The runtime uses
the active connection for every request.

**Lifecycle:** [Connection Management](10-connection-management.md).

### Connection Field

One input in the connection form. Declared in the manifest, rendered
dynamically by the Store UI. Includes type (`string`, `password`, `select`,
`file`, ...), validation rules, default value, and optional conditional
visibility.

**Full reference:** [Connection Fields](05-connection-fields.md).

### Runtime Hint

A non-functional declaration that only affects the Store UI — e.g., "this
provider requires a custom TLS certificate" or "tokens expire every 12 hours".
Runtime hints do NOT enforce anything; the adapter code is the source of truth
for actual behavior. They exist so users understand what to expect before they
save a connection.

**Full reference:** [Runtime Hints](07-runtime-hints.md).

### Permission

A declaration of what a package's adapter is allowed to do at runtime:
`network.connect`, `secrets.read:connection`, `llm.generate`,
`llm.stream`, `models.discover`, `usage.read`. Three permissions are
**forbidden** and cannot be declared:
`workspace.read`, `workspace.write`, `shell.execute`. A package that tries to
declare them will fail install validation.

**Full reference:** [Permissions](08-permissions.md).

### Plugin Host

The main-process component that spawns and manages a **worker subprocess** for
every `native` / `custom` package. It enforces the network allowlist, isolates
dependencies in a per-package venv, and translates errors between the IPC
boundary and the canonical `ProviderError` taxonomy.

**Full reference:** [Architecture](01-architecture.md#plugin-host).

### Provider Registry

The on-disk `registry.json` file that tracks every installed package, its
version history, and its active version. Reading this file (via
`provider_registry.find(id)`) is how the runtime decides whether a given
`provider_id` refers to a built-in or a package.

**Full reference:** [Installation Flow](09-installation-flow.md).

---

## Reading paths

### Path 1 — "I want to add a new provider fast" (30 min total)

1. [Quickstart — Compatible](02-quickstart-compatible.md) *(5 min read)*
2. [Manifest Reference](04-manifest-reference.md) — sections relevant to your provider *(10 min)*
3. [Connection Fields](05-connection-fields.md) — for custom auth *(10 min)*
4. [Testing](09-installation-flow.md#testing-a-package) *(5 min)*
5. **Done.** You have a working package.

### Path 2 — "My provider needs real code" (2-4 hours)

1. [Architecture](01-architecture.md) *(15 min)*
2. [Quickstart — Native](03-quickstart-native.md) *(20 min)*
3. [Manifest Reference](04-manifest-reference.md) — complete *(20 min)*
4. [Adapter Protocol](06-adapter-protocol.md) *(30 min)*
5. [Runtime Hints](07-runtime-hints.md) *(10 min)*
6. [Permissions](08-permissions.md) *(10 min)*
7. [Examples](13-examples.md) — GigaChat, AWS Bedrock, Yandex *(30 min)*
8. **Build your adapter.** *(60-120 min)*
9. [Testing](09-installation-flow.md#testing-a-package) *(10 min)*

### Path 3 — "I need to understand everything before I trust this" (4-6 hours)

Read the docs in numerical order. Each one builds on the previous. The
[Architecture](01-architecture.md) doc has a full system diagram at the top.

---

## Documentation index

| # | Document | Length | Purpose |
|---|----------|--------|---------|
| 01 | [Architecture](01-architecture.md) | ~500 lines | System diagram, component roles, data flow |
| 02 | [Quickstart — Compatible](02-quickstart-compatible.md) | ~200 lines | Build a working package in 5 minutes (no code) |
| 03 | [Quickstart — Native](03-quickstart-native.md) | ~350 lines | Build a native provider with full Python adapter |
| 04 | [Manifest Reference](04-manifest-reference.md) | ~400 lines | Complete `manifest.json` field reference |
| 05 | [Connection Fields](05-connection-fields.md) | ~350 lines | Complete `connection_fields` reference |
| 06 | [Adapter Protocol](06-adapter-protocol.md) | ~450 lines | `ProviderAdapter` contract, methods, errors |
| 07 | [Runtime Hints](07-runtime-hints.md) | ~200 lines | `runtime_hints` reference and UI integration |
| 08 | [Permissions](08-permissions.md) | ~250 lines | Permission model, enforcement, forbidden permissions |
| 09 | [Installation Flow](09-installation-flow.md) | ~350 lines | What happens during install, security checks, testing |
| 10 | [Connection Management](10-connection-management.md) | ~300 lines | Multiple connections, active selection, testing |
| 11 | [API Reference](11-api-reference.md) | ~400 lines | Every REST endpoint with curl examples |
| 12 | [Security Model](12-security-model.md) | ~300 lines | What is enforced, what is not, threat model |
| 13 | [Examples](13-examples.md) | ~400 lines | GigaChat, Yandex, AWS Bedrock, OpenRouter, Vertex |
| 14 | [Troubleshooting](14-troubleshooting.md) | ~250 lines | Common errors, diagnostics, how to get help |

---

## 30-second quickstart

The simplest possible Provider Package — a compatible provider for a
hypothetical "Acme AI" that speaks the OpenAI Chat Completions protocol.

**1. Create `manifest.json`:**

```json
{
  "schema_version": "1.0.0",
  "kind": "peldrun.llm_provider",
  "id": "acme-ai",
  "name": "Acme AI",
  "version": "1.0.0",
  "description": "Acme AI provides fast, affordable LLM inference.",
  "api_contract": "peldrun.llm.provider.v1",
  "transport": "compatible",
  "base_adapter": "openai_compat",
  "author": "Acme Inc.",
  "homepage": "https://acme.example.com",
  "license": "MIT",
  "connection_fields": [],
  "features": {
    "native_streaming": true,
    "tool_calling": true,
    "usage_reporting": true,
    "reasoning": true
  },
  "permissions": {
    "network": "connection_endpoint",
    "secrets": ["connection.api_key"],
    "workspace_access": false,
    "shell_execute": false
  }
}
```

**2. Zip it:**

```bash
zip acme-ai-1.0.0.zip manifest.json
```

**3. Install it:**

- Open the PELDRUN UI → **Store** → **Providers** → **Upload Package**
- Select `acme-ai-1.0.0.zip`
- The package appears in the **Installed** tab with a "Not configured" badge
- Click **Configure** → fill in the API endpoint and key → **Create Connection**
- Click **Make active** → you can now use Acme AI in chat

**4. Use it in chat:**

The package is registered as provider ID `acme-ai`. Any chat using this
provider ID will be routed through the `openai_compat` adapter, which speaks
the standard OpenAI protocol that Acme AI implements.

That's it. No Python code, no PELDRUN modifications, no release cycle.

For a provider with custom authentication (e.g., GigaChat's OAuth scope), see
[Connection Fields](05-connection-fields.md).

For a provider that speaks a non-OpenAI protocol (e.g., AWS Bedrock), see
[Quickstart — Native](03-quickstart-native.md).

---

## Support

- **Bug reports and feature requests**: GitHub Issues on the PELDRUN repository
- **Security disclosures**: See the root `SECURITY.md` in the repository
- **Questions and discussion**: GitHub Discussions

---

## Versioning

This documentation tracks **provider API contract version 1**
(`peldrun.llm.provider.v1`). Packages that declare this contract will work
with every PELDRUN release that supports v1. A future breaking change will
bump the contract to `v2` and ship alongside a migration guide; packages
declaring v1 will continue to work until they are explicitly removed (with
advance notice).

Schema changes (additive fields, new connection field types, new transport
options) do NOT bump the contract version. They are backward-compatible by
design — the schema uses `extra="forbid"` for the manifest but stores
snapshots in the registry as plain dicts, so future field additions cannot
break existing packages.

---

## What's next

- **New to PELDRUN?** → [Architecture](01-architecture.md)
- **Want to build a package?** → [Quickstart — Compatible](02-quickstart-compatible.md)
- **Need the full manifest schema?** → [Manifest Reference](04-manifest-reference.md)
```
