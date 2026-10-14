# PELDRUN Search Subsystem — Provider Support Matrix & Architecture Contract

## 1. Orchestration & Execution Model

- **Default Strategy:** First-Success Failover (`SearchProviderFactory.execute_orchestrated_search`).
- **Single-Provider Mode:** Setting `fallback_engines = []` in `config/config.toml` is a valid, first-class configuration. When empty, PELDRUN queries **only** the selected primary `engine` and never injects `DuckDuckGo` or any other fallback silently.
- **Unknown Providers:** Any unknown provider name raises `UnknownProviderError` immediately (`ProviderStatus.CONFIGURATION_ERROR`) without making outbound network calls.
- **Global Time Budget:** Enforced via a monotonic deadline (`total_budget_seconds`, default `22.0s`, strictly below `WebSearchTool`'s `25.0s` outer timeout). Every provider call is wrapped in `asyncio.wait_for(..., timeout=remaining)`.
- **No Application-Level Result Cache:** Live web searches are executed directly without an application-level result cache to preserve freshness and local simplicity.

---

## 2. Provider Support Matrix

| Provider | Config Key | Modes / Endpoint | Credentials Required | Max Results / Query | Locale (`lang` / `country`) | Diagnostic Behavior | Cost Basis & Default Estimate | Lifecycle & Official Docs |
|---|---|---|---|---|---|---|---|---|
| **DuckDuckGo** | `[search.duckduckgo]` | `ddgs` library (`auto` / `ddgs` / `html`) | None | `20` | Yes (`region` e.g. `us-en`, `safesearch`: `on`/`moderate`/`off`) | Transport reachability (`GET https://duckduckgo.com/`); non-billable | `assumed_zero_local` (`$0.0000`) | **Supported** — [ddgs GitHub](https://github.com/deedy5/ddgs) |
| **SearXNG** | `[search.searxng]` | `GET <base_url>/search?format=json` | `base_url` (Instance must enable `json` in `search.formats`) | `20` | `language` + `safesearch` (`0..2`) | Two-stage check: `/config` + `/search?format=json` (catches HTTP 403); non-billable | `assumed_zero_local` (`$0.0000`, or `configured_estimate` if overridden) | **Supported** — [SearXNG Search API](https://docs.searxng.org/dev/search_api.html) |
| **Tavily AI** | `[search.tavily]` | `POST https://api.tavily.com/search` | `api_key` (`Authorization: Bearer`) | `20` | Not parameterized in standard payload | Minimal search probe (**consumes 1 API credit**) | `provider_reported` (`usage.credits` × `$0.0050`) | **Supported** — [Tavily Search API](https://docs.tavily.com/documentation/api-reference/endpoint/search) |
| **Brave Search** | `[search.brave]` | `GET https://api.search.brave.com/res/v1/web/search` | `api_key` (`X-Subscription-Token`) | `20` (Hard cap) | `search_lang`, `country` | Minimal search probe (**consumes 1 request quota**) | `configured_estimate` (`$0.0050` / request) | **Supported** — [Brave Web Search API](https://api.search.brave.com/app/documentation/web-search/get-started) |
| **Google CSE** | `[search.google]` | `GET https://www.googleapis.com/customsearch/v1` | `api_key` + `cx` (Search Engine ID) | `10` (Hard cap) | `hl` (lang), `gl` (country) | Minimal CSE query (**consumes 1 daily quota unit**) | `configured_estimate` (`$0.0050` / request) | **Legacy (Retires 2027-01-01)** — [Google CSE Docs](https://developers.google.com/custom-search/v1/overview) |
| **Jina Search** | `[search.jina]` | `POST https://s.jina.ai/` | `api_key` (`Authorization: Bearer`; optional if `allow_no_key=true`) | `20` (Hard cap) | `hl` (lang), `gl` (country) | Minimal search probe (**billable when authenticated**; 401/402/429 fail explicitly) | `configured_estimate` (`$0.0020` / request; tracks `usage.tokens`) | **Supported** — [Jina API Docs](https://api.jina.ai/docs) |
| **Yandex Search** | `[search.yandex]` | `cloud_v2`: `POST https://searchapi.api.cloud.yandex.net/v2/web/search` (Base64 `rawData`)<br>`classic_xml`: `GET https://yandex.com/search/xml` | `api_key` + `folder_id` (required for `cloud_v2`) | `15` | `searchType` (`RU`/`TR`/`KK`/`COM`), `region` | Mode-aware search probe (**consumes 1 query unit**; HTTP 400/401/403 fail explicitly) | `configured_estimate` (`$0.0030` / request) | **Supported** — [Yandex Cloud SyncSearch](https://yandex.cloud/en/docs/search-api/api-ref/SyncSearch) |

---

## 3. Google Custom Search Legacy & Migration Note

Google's official documentation states that the **Custom Search JSON API** is closed to new customers and is scheduled for discontinuation on **January 1, 2027**.
- PELDRUN marks Google CSE as `legacy` in the Settings UI and capability descriptors.
- Existing accounts with valid `api_key` and `cx` credentials remain functional until the retirement date, but users should plan migration to **Brave Search**, **Tavily AI**, or a self-hosted **SearXNG** instance.

---

## 4. Untrusted External Content & Local Ledger Privacy

1. **Untrusted Content Boundary:** All search titles, URLs, snippets, and optional raw content previews are validated (`http`/`https` only, hostname required, no embedded credentials, bounded character lengths) and wrapped inside `<untrusted_search_results>` delimiters when returned to the agent.
2. **Local Usage Ledger (`peldrun_search_usage.db`):**
   - Stored locally under `backend/storage/peldrun_search_usage.db` (or `storage/peldrun_search_usage.db`).
   - Records `provider`, `status`, `request_count`, `usage_units`, `cost_nano_usd`, `cost_basis`, `latency_ms`, `query_hash`, and `query`.
   - **Privacy Mode (`privacy_mode = true`):** When enabled in Search Settings, plain query strings are redacted (`[REDACTED_PRIVACY_MODE]`) and only the SHA-256 `query_hash` and execution metadata are persisted.
   - **Deletion:** Users can purge all recorded rows at any time via the **Clear Ledger** button in the Search Intelligence Center (`DELETE /api/search/metrics`).
