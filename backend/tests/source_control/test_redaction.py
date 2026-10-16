from __future__ import annotations

from typing import Any, Mapping
import pytest

# Resilient imports for redaction utilities
try:
    from ...integrations.source_control.security.redaction import (
        redact_data,
        redact_text,
        redact_url,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.security.redaction import (
        redact_data,
        redact_text,
        redact_url,
    )


# ---------------------------------------------------------------------------
# 1. Text Redaction Tests (Tokens, Headers, Keys)
# ---------------------------------------------------------------------------


def test_redact_text_github_tokens() -> None:
    """Verifies that all standard GitHub token prefixes are identified and redacted."""
    raw_text = (
        "Found token ghp_1234567890abcdefghijklmnopqrstuvwxyzAB in config, "
        "along with oauth gho_abcdefghijklmnopqrstuvwxyz1234567890AB and "
        "server token ghs_9876543210zyxwvutsrqponmlkjihgfedcbaAB."
    )
    cleaned = redact_text(raw_text)

    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in cleaned
    assert "gho_abcdefghijklmnopqrstuvwxyz1234567890AB" not in cleaned
    assert "ghs_9876543210zyxwvutsrqponmlkjihgfedcbaAB" not in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_text_gitlab_and_generic_bearer_tokens() -> None:
    """Ensures GitLab personal tokens and Authorization Bearer headers are redacted."""
    raw_text = (
        "GitLab PAT: glpat-abcdefghijklmnopqrst123456\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDN\n"
        "Regular text remains visible."
    )
    cleaned = redact_text(raw_text)

    assert "glpat-abcdefghijklmnopqrst123456" not in cleaned
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDN" not in cleaned
    assert "Regular text remains visible." in cleaned
    assert "[REDACTED]" in cleaned


def test_redact_text_private_key_blocks() -> None:
    """Ensures RSA and generic private key blocks are fully redacted from text."""
    raw_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y1p4Q5eM8j7...fakekeycontent...==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    raw_text = f"Deployment key details:\n{raw_key}\nEnd of log."
    cleaned = redact_text(raw_text)

    assert "fakekeycontent" not in cleaned
    assert "-----BEGIN RSA PRIVATE KEY-----" not in cleaned
    assert "[REDACTED_PRIVATE_KEY]" in cleaned or "[REDACTED]" in cleaned
    assert "End of log." in cleaned


def test_redact_text_preserves_safe_content() -> None:
    """Verifies that non-secret strings and common technical terms remain unchanged."""
    safe_text = "Commit feat(auth): update oauth2 callback handler and fix issue #42."
    assert redact_text(safe_text) == safe_text

    code_snippet = "def calculate_hash(data: str) -> str:\n    return hashlib.sha256(data.encode()).hexdigest()"
    assert redact_text(code_snippet) == code_snippet


# ---------------------------------------------------------------------------
# 2. URL Redaction Tests
# ---------------------------------------------------------------------------


def test_redact_url_with_embedded_credentials() -> None:
    """Ensures tokens and basic auth passwords in Git URLs are stripped or masked."""
    test_urls = [
        (
            "https://ghp_secretToken12345@github.com/peldrun/sample-repo.git",
            "github.com/peldrun/sample-repo.git",
        ),
        (
            "https://x-access-token:ghs_sessionToken6789@github.com/owner/project",
            "github.com/owner/project",
        ),
        (
            "https://bot_user:super_secret_pass@gitlab.com/group/repo.git",
            "gitlab.com/group/repo.git",
        ),
    ]

    for raw_url, expected_suffix in test_urls:
        cleaned = redact_url(raw_url)
        assert "ghp_secretToken12345" not in cleaned
        assert "ghs_sessionToken6789" not in cleaned
        assert "super_secret_pass" not in cleaned
        assert expected_suffix in cleaned


def test_redact_url_clean_urls_unaffected() -> None:
    """Verifies that standard URLs without userinfo credentials are not mutated."""
    clean_urls = [
        "https://github.com/peldrun/peldrun",
        "https://gitlab.com/peldrun-core/subsystem.git",
        "https://api.github.com/repos/owner/repo/pulls/10",
        "git@github.com:peldrun/peldrun.git",
    ]
    for url in clean_urls:
        assert redact_url(url) == url


def test_redact_url_handles_empty_and_invalid_inputs() -> None:
    """Ensures redact_url safely handles empty strings or malformed URLs."""
    assert redact_url("") == ""
    assert redact_url("   ") == ""
    assert redact_url("not_a_valid_url") == "not_a_valid_url"


# ---------------------------------------------------------------------------
# 3. Structured Data Redaction Tests (redact_data)
# ---------------------------------------------------------------------------


def test_redact_data_nested_dictionary() -> None:
    """Ensures dictionary keys indicating secrets have their values replaced, and strings inside are sanitized."""
    payload: dict[str, Any] = {
        "repository": "peldrun/peldrun",
        "status": "connected",
        "credentials": {
            "access_token": "ghp_realSecretToken9876543210",
            "refresh_token": "ghr_refreshSecret12345",
            "client_secret": "raw_client_secret_xyz",
            "password": "user_super_password",
        },
        "metadata": {
            "description": "Repo with clone url https://ghp_inlineToken@github.com/repo.git",
            "counts": [1, 2, 3],
        },
    }

    sanitized = redact_data(payload)

    # Verify sensitive keys are masked
    assert sanitized["credentials"]["access_token"] == "[REDACTED]"
    assert sanitized["credentials"]["refresh_token"] == "[REDACTED]"
    assert sanitized["credentials"]["client_secret"] == "[REDACTED]"
    assert sanitized["credentials"]["password"] == "[REDACTED]"

    # Verify inline secrets in nested strings are redacted
    assert "ghp_inlineToken" not in sanitized["metadata"]["description"]

    # Verify non-secret fields remain untouched
    assert sanitized["repository"] == "peldrun/peldrun"
    assert sanitized["status"] == "connected"
    assert sanitized["metadata"]["counts"] == [1, 2, 3]


def test_redact_data_sequence_of_dicts() -> None:
    """Ensures lists of dictionaries are recursively sanitized."""
    items = [
        {"id": 1, "token": "ghp_secret_1"},
        {"id": 2, "api_key": "secret_api_key_2"},
        {"id": 3, "name": "regular_item"},
    ]

    sanitized = redact_data(items)

    assert isinstance(sanitized, list)
    assert sanitized[0]["token"] == "[REDACTED]"
    assert sanitized[1]["api_key"] == "[REDACTED]"
    assert sanitized[2]["name"] == "regular_item"


def test_redact_data_primitives_passthrough() -> None:
    """Ensures primitive values (None, ints, bools, floats) pass through safely."""
    assert redact_data(None) is None
    assert redact_data(123) == 123
    assert redact_data(True) is True
    assert redact_data(45.67) == 45.67
