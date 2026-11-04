# backend/omweb/agent_bridge_parts/lmstudio.py
"""
LM Studio readiness probe.

Checks whether the configured model is reachable and loaded into memory
in the local LM Studio server, before the bridge attempts a job run.

Endpoint normalization
----------------------
LM Studio exposes three distinct surfaces:

    * ``/v1/...``        — OpenAI-compatible shim (chat completions, models)
    * ``/api/v0/...``    — native API (chat with reasoning, stats, model info)
    * ``/api/v1/...``    — newer native API (some versions only)

Callers routinely pass the base URL with one of these suffixes attached
(e.g. ``http://host:1234/v1`` or ``http://host:1234/api/v1``). Building
the probe URL by naively stripping ``/v1`` produced double-suffixed
paths like ``/api/api/v0/models`` when the caller's base URL already
contained ``/api``. ``_normalize_lmstudio_root`` handles every known
combination idempotently.
"""

from __future__ import annotations

import re
from typing import Any, Dict

import httpx


# Matches any endpoint suffix that a caller might append to LM Studio's
# root URL. The alternation is ordered longest-first so that
# ``/api/v0`` matches before ``/v0`` would (if it existed), preventing
# partial matches from leaving a stray segment behind.
_STRIPPABLE_SUFFIXES = re.compile(
    r"/(?:api/v0|api/v1|openai/v1|openai|v1beta|v1|api)/?$",
    re.IGNORECASE,
)


def _normalize_lmstudio_root(base_url: str) -> str:
    """Return the LM Studio server root URL with any endpoint suffix removed.

    Handles every form a user or caller might enter:

        http://127.0.0.1:1234
        http://127.0.0.1:1234/
        http://127.0.0.1:1234/v1
        http://127.0.0.1:1234/api
        http://127.0.0.1:1234/api/v0
        http://127.0.0.1:1234/api/v1
        http://127.0.0.1:1234/api/api/v0      (legacy double-suffix bug)

    The previous implementation used ``str.replace("/v1", "")``, which
    only stripped the literal ``/v1`` substring. When the caller's base
    URL already contained ``/api`` (e.g. ``.../api/v1``), the result was
    ``.../api`` — and appending ``/api/v0/models`` produced the double
    ``/api/api/v0/models`` path seen in the LM Studio server logs.
    """
    cleaned = (base_url or "").strip().rstrip("/")

    # Collapse any literal double /api/ left behind by an earlier bug.
    while "/api/api/" in cleaned:
        cleaned = cleaned.replace("/api/api/", "/api/")

    # Strip known suffixes, possibly repeated (e.g. "/v1/api/v1").
    while True:
        new = _STRIPPABLE_SUFFIXES.sub("", cleaned)
        if new == cleaned or not new:
            break
        cleaned = new.rstrip("/")

    return cleaned


async def check_lmstudio_model_readiness(
    base_url: str, model_name: str, api_key: str = ""
) -> Dict[str, Any]:
    """Probe LM Studio's REST API to check model reachability and load state.

    Tries both the v1 and v0 model listing endpoints (2-second timeout each).

    Args:
        base_url:   Base URL of the LM Studio server. May include a
                    trailing ``/v1``, ``/api/v0``, or ``/api/v1`` suffix.
                    All known suffixes are normalized before probing.
        model_name: Model identifier to look up (case-insensitive, substring match).
        api_key:    Optional API key. Placeholder/masked keys ("••••" or "EMPTY")
                    are ignored and no Authorization header is sent.

    Returns:
        A dict with keys:
            - found:          True if the model id was found in LM Studio.
            - is_loaded:      True if a loaded instance is active.
            - context_length: Context length of the first loaded instance (or None).
            - unreachable:    True if the server could not be reached at all.
    """
    clean_base = _normalize_lmstudio_root(base_url)
    headers = {"Accept": "application/json"}
    if api_key and "••••" not in api_key and api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {api_key}"

    endpoints = [
        f"{clean_base}/api/v1/models",
        f"{clean_base}/api/v0/models",
    ]

    for ep in endpoints:
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                resp = await client.get(ep, headers=headers)
                if resp.status_code == 200:
                    data = resp.json()
                    models = data.get("models") or data.get("data") or []
                    for m in models:
                        mid = str(m.get("key") or m.get("id") or "")
                        if mid.lower() == model_name.lower() or model_name.lower() in mid.lower():
                            loaded_instances = m.get("loaded_instances") or []
                            is_loaded = len(loaded_instances) > 0
                            ctx_len = (
                                loaded_instances[0].get("config", {}).get("context_length")
                                if is_loaded else None
                            )
                            return {
                                "found": True,
                                "is_loaded": is_loaded,
                                "context_length": ctx_len,
                                "unreachable": False,
                            }
                    return {"found": False, "is_loaded": False, "unreachable": False}
        except Exception:
            continue

    return {"found": False, "is_loaded": False, "unreachable": True}
