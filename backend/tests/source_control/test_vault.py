# backend/tests/source_control/test_vault.py
from __future__ import annotations

import pytest
from .conftest import InMemoryVault


def test_put_and_get_secret() -> None:
    vault = InMemoryVault()
    vault._put("github_token_1", "ghp_secret123")

    assert vault._get("github_token_1") == "ghp_secret123"


def test_get_nonexistent_key() -> None:
    vault = InMemoryVault()
    assert vault._get("unknown_key") is None


def test_overwrite_secret() -> None:
    vault = InMemoryVault()
    vault._put("my_key", "old_secret")
    vault._put("my_key", "new_secret")

    assert vault._get("my_key") == "new_secret"


def test_delete_secret() -> None:
    vault = InMemoryVault()
    vault._put("token_to_delete", "secret_value")
    assert vault._get("token_to_delete") == "secret_value"

    vault._delete("token_to_delete")
    assert vault._get("token_to_delete") is None


def test_delete_nonexistent_key() -> None:
    vault = InMemoryVault()
    vault._delete("non_existent_key")


def test_clear_vault() -> None:
    vault = InMemoryVault()
    vault._put("key1", "secret1")
    vault._put("key2", "secret2")

    vault.clear()

    assert vault._get("key1") is None
    assert vault._get("key2") is None


def test_vault_isolation() -> None:
    vault1 = InMemoryVault()
    vault2 = InMemoryVault()

    vault1._put("shared_key", "secret_from_vault1")

    assert vault1._get("shared_key") == "secret_from_vault1"
    assert vault2._get("shared_key") is None
