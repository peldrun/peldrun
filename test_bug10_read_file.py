# test_bug10_read_file.py
"""
Standalone tests for the Bug 10 fix in SourceControlFacade.read_file.

Verifies that:
    1. An explicit `ref` is passed through to the adapter verbatim.
    2. `ref=None` with a usable `default_branch` uses that branch
       and passes it explicitly to the adapter.
    3. `ref=None` with an empty `default_branch` raises a clear
       ValidationError instead of silently reporting `ref=""`.
    4. `ref=None` with a missing `default_branch` raises a clear
       ValidationError instead of defaulting to "main".

Run from the project root:
    python test_bug10_read_file.py
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path as _Path
from typing import Optional

# `SourceControlFacade` uses absolute `from integrations.source_control
# import ...` statements, so `backend/` MUST be on `sys.path` for the
# module to load. Production launchers do this implicitly; the test
# does it here so it can run standalone.
#
# CRITICAL: The model, enum, and error imports below MUST use the
# SAME module path that the facade uses internally. The facade's
# `try: from integrations.source_control import ...` block succeeds
# once `backend/` is on `sys.path`, which means its `RepositoryRef`
# is `integrations.source_control.domain.models.RepositoryRef`.
#
# If the test imported `backend.integrations.source_control.domain
# .models.RepositoryRef`, Python would register that file under a
# DIFFERENT key in `sys.modules`, creating a SECOND class object
# for the same source file. `isinstance(x, RepositoryRef)` would
# then return False across the two classes, and the facade's
# `_resolve_repo` would silently fall through to a `NotFoundError`
# even though the adapter returned the right object.
#
# Aligning both sides on `integrations.*` avoids that dual-import
# trap entirely. This ALSO works around the facade's broken
# `except ImportError` fallback (which repeats the same path as the
# `try` block instead of using `backend.integrations.*`), because
# the fallback is never reached.
_BACKEND_ROOT = _Path(__file__).resolve().parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from backend.omweb.services.source_control_facade import (
    SourceControlFacade,
)
from integrations.source_control.domain.models import (
    RepositoryRef,
)
from integrations.source_control.domain.enums import ProviderId
from integrations.source_control.errors import ValidationError


def make_repo(default_branch: Optional[str]) -> RepositoryRef:
    """Build a RepositoryRef with the given default_branch value."""
    return RepositoryRef(
        provider=ProviderId.GITHUB,
        account_id="test-account",
        repository_id="test/repo",
        namespace="test",
        name="repo",
        clone_url="https://github.com/test/repo.git",
        web_url="https://github.com/test/repo",
        default_branch=default_branch,
        is_private=False,
    )


class FakeAdapter:
    """Minimal adapter used to exercise read_file's ref resolution."""

    def __init__(self, repo: RepositoryRef) -> None:
        self._repo = repo
        self.last_read_ref: Optional[str] = "UNSET"
        self.get_repository_calls = 0

    async def get_repository(self, repo=None, token=None):
        self.get_repository_calls += 1
        return self._repo

    async def list_repositories(
        self, page=1, page_size=30, token=None, query=None
    ):
        return [self._repo]

    async def read_file(
        self, repo, file_path, ref=None, token=None
    ):
        self.last_read_ref = ref
        return "file content"


class FakeRegistry:
    def __init__(self, adapter: FakeAdapter) -> None:
        self._adapter = adapter

    def get(self, provider):
        return self._adapter


def make_facade(adapter: FakeAdapter) -> SourceControlFacade:
    """Construct a facade wired to the given fake adapter."""
    facade = SourceControlFacade()
    facade.registry = FakeRegistry(adapter)  # type: ignore[assignment]

    async def fake_require_token(provider_id):
        return "test-token"

    facade._require_token = fake_require_token  # type: ignore[assignment]
    return facade


async def test_explicit_ref() -> None:
    """Case 1: caller supplies an explicit ref."""
    adapter = FakeAdapter(make_repo("develop"))
    facade = make_facade(adapter)
    resp = await facade.read_file(
        repository_id="test/repo",
        file_path="README.md",
        ref="feature/x",
    )
    assert resp.ref == "feature/x", f"response ref={resp.ref!r}"
    assert adapter.last_read_ref == "feature/x", (
        f"adapter got ref={adapter.last_read_ref!r}"
    )
    print(f"[PASS] explicit_ref: ref={resp.ref!r}")


async def test_inferred_default_branch() -> None:
    """Case 2: ref=None, repo has a valid default_branch."""
    adapter = FakeAdapter(make_repo("develop"))
    facade = make_facade(adapter)
    resp = await facade.read_file(
        repository_id="test/repo",
        file_path="README.md",
    )
    assert resp.ref == "develop", f"response ref={resp.ref!r}"
    assert adapter.last_read_ref == "develop", (
        f"adapter got ref={adapter.last_read_ref!r}"
    )
    print(f"[PASS] inferred_default_branch: ref={resp.ref!r}")


async def test_empty_default_branch_raises() -> None:
    """Case 3: ref=None, default_branch is empty string."""
    adapter = FakeAdapter(make_repo(""))
    facade = make_facade(adapter)
    try:
        await facade.read_file(
            repository_id="test/repo",
            file_path="README.md",
        )
    except ValidationError as exc:
        print(
            f"[PASS] empty_default_branch_raises: "
            f"{type(exc).__name__}"
        )
        return
    raise AssertionError(
        "Expected ValidationError for empty default_branch, "
        f"but the call returned; adapter.last_read_ref="
        f"{adapter.last_read_ref!r}"
    )


async def test_missing_default_branch_raises() -> None:
    """Case 4: ref=None, default_branch is None."""
    adapter = FakeAdapter(make_repo(None))
    facade = make_facade(adapter)
    try:
        await facade.read_file(
            repository_id="test/repo",
            file_path="README.md",
        )
    except ValidationError as exc:
        print(
            f"[PASS] missing_default_branch_raises: "
            f"{type(exc).__name__}"
        )
        return
    raise AssertionError(
        "Expected ValidationError for None default_branch, "
        f"but the call returned; adapter.last_read_ref="
        f"{adapter.last_read_ref!r}"
    )


async def main() -> None:
    print("=" * 60)
    print("Bug 10 read_file Tests")
    print("=" * 60)
    await test_explicit_ref()
    await test_inferred_default_branch()
    await test_empty_default_branch_raises()
    await test_missing_default_branch_raises()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(2)
