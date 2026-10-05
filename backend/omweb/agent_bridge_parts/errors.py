"""
Smart error message formatting for the PELDRUN Universal Agent Bridge.

Turns raw exception objects into user-friendly markdown "error cards"
with actionable hints, matching the original behavior of agent_bridge.py.
"""

from __future__ import annotations


def _err_card(title: str, description: str, hint: str = "", code: str = "") -> str:
    """Build a markdown error card with optional hint and code block.

    Args:
        title:       Short headline shown with a warning emoji.
        description: Main body of the error explanation.
        hint:        Optional actionable hint line prefixed with a light bulb.
        code:        Optional raw code/error text placed inside a fenced block.

    Returns:
        A newline-joined markdown string.
    """
    parts = [f"### ⚠️ {title}", "", description]
    if hint:
        parts += ["", f"> 💡 **Hint:** {hint}"]
    if code:
        parts += ["", "```", code, "```"]
    return "\n".join(parts)


def format_smart_error(err: Exception, model_name: str, provider_name: str) -> str:
    """Translate a raw exception into a friendly, human-readable error card.

    Handles common failure modes:
        - LM Studio engine crash / model not loaded
        - Connection refused on LM Studio (port 1234) or Ollama (port 11434)
        - Context length exceeded
        - Invalid API key / authentication failures

    Falls back to a generic "Chat execution error" card for unknown errors.

    Args:
        err:           The exception that was raised.
        model_name:    Display name of the model in use.
        provider_name: Display name of the provider in use.

    Returns:
        A markdown string suitable for direct display in the chat UI.
    """
    err_str = str(err)
    lower_err = err_str.lower()

    if (
        "failed to load model" in lower_err
        or "failed to load" in lower_err
        or "engine protocol predict request failed" in lower_err
        or "fetch failed" in lower_err
    ):
        return _err_card(
            title=f"Model `{model_name}` engine is not responding in LM Studio",
            description=(
                "LM Studio internal engine either crashed (GPU Out-Of-Memory) "
                "or the model is currently not loaded into memory."
            ),
            hint=(
                "Open LM Studio, navigate to the Local Server tab, and click 'Reload' "
                "(or eject and re-load the model) to refresh the engine memory."
            ),
            code=err_str if "fetch failed" in lower_err else ""
        )

    if "connection refused" in lower_err or "connecterror" in lower_err or "10061" in lower_err:
        if "1234" in err_str or "lmstudio" in provider_name.lower():
            return _err_card(
                title="Could not connect to the local LM Studio server",
                description="The LM Studio local server on **port 1234** is unreachable.",
                hint="Make sure the LM Studio application is running and the Local Server is enabled.",
            )
        if "11434" in err_str or "ollama" in provider_name.lower():
            return _err_card(
                title="Could not connect to the local Ollama server",
                description="The Ollama service on **port 11434** is unreachable.",
                hint="Make sure the Ollama service is running on your machine.",
            )

    if "context_length_exceeded" in lower_err or "maximum context length" in lower_err:
        return _err_card(
            title=f"Context length exceeded for model `{model_name}`",
            description="The conversation has exceeded the maximum context length of this model.",
            hint="Start a new conversation or reduce the size of the input.",
        )

    if "invalid_api_key" in lower_err or "incorrect api key" in lower_err or "401" in lower_err:
        return _err_card(
            title=f"API key authentication failed for provider `{provider_name}`",
            description="The API key was rejected by the provider.",
            hint="Verify the key is correct in Settings > Cloud Providers.",
        )

    return _err_card(
        title="Chat execution error",
        description=f"**Provider:** `{provider_name}` — **Model:** `{model_name}`",
        code=err_str,
    )