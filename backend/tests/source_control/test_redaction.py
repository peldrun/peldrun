# backend/tests/source_control/test_redaction.py
from __future__ import annotations

from typing import Any
import pytest

try:
    from ...integrations.source_control.security.redaction import (
        REDACTED,
        redact_data,
        redact_text,
        redact_url,
        contains_secret,
        is_likely_secret,
        SafeStr,
    )
except (ImportError, ValueError):
    from backend.integrations.source_control.security.redaction import (
        REDACTED,
        redact_data,
        redact_text,
        redact_url,
        contains_secret,
        is_likely_secret,
        SafeStr,
    )


def test_redact_text_github_tokens() -> None:
    raw_text = (
        "Found token ghp_1234567890abcdefghijklmnopqrstuvwxyzAB in config, "
        "along with oauth gho_abcdefghijklmnopqrstuvwxyz1234567890AB and "
        "server token ghs_9876543210zyxwvutsrqponmlkjihgfedcbaAB."
    )
    cleaned = redact_text(raw_text)

    assert "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB" not in cleaned
    assert "gho_abcdefghijklmnopqrstuvwxyz1234567890AB" not in cleaned
    assert "ghs_9876543210zyxwvutsrqponmlkjihgfedcbaAB" not in cleaned
    assert REDACTED in cleaned


def test_redact_text_gitlab_and_generic_bearer_tokens() -> None:
    raw_text = (
        "GitLab PAT: glpat-abcdefghijklmnopqrst123456\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDN\n"
        "Regular text remains visible."
    )
    cleaned = redact_text(raw_text)

    assert "glpat-abcdefghijklmnopqrst123456" not in cleaned
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.e30.t-IDN" not in cleaned
    assert "Regular text remains visible." in cleaned
    assert REDACTED in cleaned


def test_redact_text_private_key_blocks() -> None:
    raw_key = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA0Y1p4Q5eM8j7...fakekeycontent...==\n"
        "-----END RSA PRIVATE KEY-----"
    )
    raw_text = f"Deployment key details:\n{raw_key}\nEnd of log."
    cleaned = redact_text(raw_text)

    assert "fakekeycontent" not in cleaned
    assert "-----BEGIN RSA PRIVATE KEY-----" not in cleaned
    assert REDACTED in cleaned
    assert "End of log." in cleaned


def test_redact_text_preserves_safe_content() -> None:
    safe_text = "Commit feat(auth): update oauth2 callback handler and fix issue #42."
    assert redact_text(safe_text) == safe_text

    code_snippet = "def calculate_hash(data: str) -> str:\n    return hashlib.sha256(data.encode()).hexdigest()"
    assert redact_text(code_snippet) == code_snippet


def test_redact_url_with_embedded_credentials() -> None:
    test_urls = [
        "https://ghp_123456789012345678901234567890@github.com/peldrun/sample-repo.git",
        "https://x-access-token:ghs_123456789012345678901234567890@github.com/owner/project",
        "https://bot_user:super_secret_pass@gitlab.com/group/repo.git",
    ]

    for raw_url in test_urls:
        cleaned = redact_url(raw_url)
        assert "ghp_123456789012345678901234567890" not in cleaned
        assert "ghs_123456789012345678901234567890" not in cleaned
        assert "super_secret_pass" not in cleaned
        assert REDACTED in cleaned
        assert "github.com" in cleaned or "gitlab.com" in cleaned


def test_redact_url_clean_urls_unaffected() -> None:
    clean_urls = [
        "https://github.com/peldrun/peldrun",
        "https://gitlab.com/peldrun-core/subsystem.git",
        "https://api.github.com/repos/owner/repo/pulls/10",
        "git@github.com:peldrun/peldrun.git",
    ]
    for url in clean_urls:
        assert redact_url(url) == url


def test_redact_url_handles_empty_and_invalid_inputs() -> None:
    assert redact_url("") == ""
    assert redact_url("   ") == "   "
    assert redact_url("not_a_valid_url") == "not_a_valid_url"



def test_redact_data_nested_dictionary() -> None:
    payload: dict[str, Any] = {
        "repository": "peldrun/peldrun",
        "status": "connected",
        "auth_details": {  # Changed from "credentials" to avoid blanket redaction
            "access_token": "ghp_123456789012345678901234567890",
            "refresh_token": "ghr_123456789012345678901234567890",
            "client_secret": "raw_client_secret_xyz",
            "password": "user_super_password",
        },
        "metadata": {
            "description": "Repo with clone url https://ghp_123456789012345678901234567890@github.com/repo.git",
            "counts": [1, 2, 3],
        },
    }

    sanitized = redact_data(payload)

    assert isinstance(sanitized, dict)
    assert sanitized["auth_details"]["access_token"] == REDACTED
    assert sanitized["auth_details"]["refresh_token"] == REDACTED
    assert sanitized["auth_details"]["client_secret"] == REDACTED
    assert sanitized["auth_details"]["password"] == REDACTED

    assert "ghp_123456789012345678901234567890" not in sanitized["metadata"]["description"]
    assert REDACTED in sanitized["metadata"]["description"]

    assert sanitized["repository"] == "peldrun/peldrun"
    assert sanitized["status"] == "connected"
    assert sanitized["metadata"]["counts"] == [1, 2, 3]




def test_redact_data_sequence_of_dicts() -> None:
    items = [
        {"id": 1, "token": "ghp_123456789012345678901234567890"},
        {"id": 2, "api_key": "secret_api_key_2"},
        {"id": 3, "name": "regular_item"},
    ]

    sanitized = redact_data(items)

    assert isinstance(sanitized, list)
    assert sanitized[0]["token"] == REDACTED
    assert sanitized[1]["api_key"] == REDACTED
    assert sanitized[2]["name"] == "regular_item"


def test_redact_data_primitives_passthrough() -> None:
    assert redact_data(None) is None
    assert redact_data(123) == 123
    assert redact_data(True) is True
    assert redact_data(45.67) == 45.67


def test_contains_secret_and_is_likely_secret() -> None:
    assert contains_secret("Here is a token: ghp_1234567890abcdefghijklmnopqrstuvwxyzAB") is True
    assert contains_secret("Just a normal string without secrets.") is False

    assert is_likely_secret("ghp_1234567890abcdefghijklmnopqrstuvwxyzAB") is True
    assert is_likely_secret("Just a normal string") is False


def test_safe_str_wrapper() -> None:
    secret_val = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
    safe = SafeStr(secret_val)

    assert safe.value == secret_val
    assert secret_val not in str(safe)
    assert secret_val not in repr(safe)
    assert REDACTED in str(safe)
