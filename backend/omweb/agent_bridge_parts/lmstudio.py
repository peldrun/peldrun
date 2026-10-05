"""
LM Studio readiness probe.

Checks whether the configured model is reachable and loaded into memory
in the local LM Studio server, before the bridge attempts a job run.
"""

from __future__ import annotations

from typing import Any, Dict

import httpx


async def check_lmstudio_model_readiness(
    base_url: str, model_name: str, api_key: str = ""
) -> Dict[str, Any]:
    """Probe LM Studio's REST API to check model reachability and load state.

    Tries both the v1 and v0 model listing endpoints (2-second timeout each).

    Args:
        base_url:   Base URL of the LM Studio server (may include trailing /v1).
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
    clean_base = base_url.replace("/v1", "").rstrip("/")
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