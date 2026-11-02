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

    # [Phase H] Out-of-memory detection must run FIRST — the OOM
    # message contains the word "failed" (e.g. "Engine protocol predict
    # stream returned an error"), and would otherwise be matched by the
    # `"failed to load"` branch below, giving a misleading hint about
    # reloading the model rather than reducing the model size or
    # context.
    if (
        "ran out of memory" in lower_err
        or "out of memory" in lower_err
        or "bad allocation" in lower_err
        or "cuda out of memory" in lower_err
        or "insufficient memory" in lower_err
        or "memory exhausted" in lower_err
        or "cannot allocate" in lower_err
    ):
        return _err_card(
            title=f"Model `{model_name}` ran out of memory",
            description=(
                f"The model **{model_name}** does not have enough "
                f"GPU/RAM to complete the generation."
            ),
            hint=(
                "In LM Studio: either eject and reload the model, "
                "reduce the context length in the model settings, or "
                "switch to a smaller quantization "
                "(e.g. Q4_K_M instead of Q8_0)."
            ),
            # The raw error (`Error code: ...`) is intentionally NOT
            # included here. For a matched OOM the formatted message
            # already contains every actionable detail; the raw dump
            # only repeats it. Full raw text remains available in the
            # backend console log and in `job_manager.get_job(id).error`
            # for debugging.
        )

    # Billing / credit exhaustion. Anthropic and OpenAI both return
    # 400/403 with variations of "credit balance" / "insufficient
    # quota" / "billing". Without this branch, the user would see a
    # generic "Chat execution error" with a raw JSON dump — less
    # actionable than a dedicated card.
    if (
        "credit balance" in lower_err
        or "insufficient quota" in lower_err
        or "insufficient credit" in lower_err
        or "billing" in lower_err and "upgrade" in lower_err
        or "exceeded your current quota" in lower_err
    ):
        return _err_card(
            title=f"Provider `{provider_name}` account has insufficient credits",
            description=(
                f"The API key for **{provider_name}** does not have "
                f"enough credit balance to complete this request."
            ),
            hint=(
                f"Visit your {provider_name} billing page to add "
                f"credits, or switch to a different provider/model "
                f"in Settings."
            ),
            # Raw error dump omitted — see rationale in the OOM branch
            # above. Backend logs contain the full payload.
        )

    # [Phase H] "Did not respond within N seconds" — emitted by the
    # new `client.py` watchdog when the provider accepts the
    # connection but never sends data.
    if (
        "did not respond within" in lower_err
        or "did not produce any output within" in lower_err
        or "stopped responding after" in lower_err
    ):
        return _err_card(
            title=f"Provider `{provider_name}` is not responding",
            description=(
                f"Model **{model_name}** did not produce any output in "
                f"time. This usually means the provider ejected the "
                f"model, crashed, or lost network connectivity."
            ),
            hint=(
                "Verify the provider is running and the model is "
                "loaded, then retry. If using LM Studio, eject and "
                "reload the model, or restart the Local Server."
            ),
            # Raw error dump omitted — see rationale in the OOM branch
            # above. Backend logs contain the full payload.
        )

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
