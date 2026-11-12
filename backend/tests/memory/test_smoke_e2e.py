# backend/tests/memory/test_smoke_e2e.py
"""
End-to-end smoke test for the P1.1-P1.7 memory stack.

Exercises the full data path WITHOUT spinning up FastAPI:

    SqliteFtsProvider (P1.3)
       + MemorySaveTool (P1.5)
       + MemorySearchTool (P1.5)
       + policies.decide_write_action (P1.4)
       + ranker + context_builder (P1.4)

The test is deliberately end-to-end on the DATA side and
unit-scoped on the HTTP side: it does not exercise the FastAPI
lifespan, the dispatcher, or the SSE bridge, all of which are
orthogonal to the correctness of the memory subsystem itself.

Coverage:
  1. Save a memory in USER scope; retrieve it.
  2. Save two memories in different PROJECT scopes; verify isolation.
  3. Save the SAME content twice; verify SKIP (dedupe).
  4. Save a superseding statement; verify SUPERSEDE.
  5. Save with READ_ONLY mode; verify rejection.
  6. Search with DISABLED mode; verify rejection (read side).
  7. Verify mark_used populated last_used_at in the DB.

Runnable:
    python tests/memory/test_smoke_e2e.py
"""

from __future__ import annotations

import sys as _sys_bootstrap
from pathlib import Path as _Path_bootstrap

if __name__ == "__main__":
    _BACKEND_ROOT = _Path_bootstrap(__file__).resolve().parents[2]
    if str(_BACKEND_ROOT) not in _sys_bootstrap.path:
        _sys_bootstrap.path.insert(0, str(_BACKEND_ROOT))

# --- Normal imports follow ----------------------------------------------

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import pytest

from peldrun.memory.domain import (
    MemoryMode,
    MemorySettings,
    default_settings,
)
from peldrun.memory.providers import SqliteFtsProvider
from peldrun.memory.tools.builtins.memory_save import MemorySaveTool
from peldrun.memory.tools.builtins.memory_search import MemorySearchTool


# pytest-asyncio runs in ``Mode.STRICT`` under this project's
# ``pytest.ini`` -- every ``async def`` test MUST carry the
# ``asyncio`` marker explicitly. Applying the marker at module level
# is equivalent to decorating each test individually, but avoids
# seven lines of repetitive boilerplate and keeps the file readable.
#
# The standalone runner at the bottom of this file uses
# ``asyncio.run()`` directly and is UNAFFECTED by this marker --
# ``pytestmark`` is a pytest-specific module attribute; the plain
# Python interpreter never reads it.
pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Harness:
    """Bundles provider + settings + tools for one test case."""

    def __init__(self, mode: MemoryMode = MemoryMode.READ_WRITE):
        self._td = Path(tempfile.mkdtemp(prefix="peldrun_smoke_"))
        self._db_path = self._td / "memory.db"
        self.settings: MemorySettings = default_settings().model_copy(
            update={"mode": mode}
        )
        self.provider: Optional[SqliteFtsProvider] = None

    async def __aenter__(self) -> "_Harness":
        self.provider = SqliteFtsProvider(db_path=self._db_path)
        await self.provider.open()
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.provider is not None:
            await self.provider.close()

    def save_tool(self) -> MemorySaveTool:
        return MemorySaveTool(
            port=self.provider, settings=self.settings
        )

    def search_tool(self) -> MemorySearchTool:
        return MemorySearchTool(
            port=self.provider, settings=self.settings
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_save_and_retrieve_user_scope() -> None:
    """Save a memory in USER scope; retrieve it by query."""
    async with _Harness() as h:
        save = h.save_tool()
        search = h.search_tool()

        result = await save.execute(
            content="User prefers dark mode in all editors",
            memory_type="preference",
            source_type="explicit_user",
            scope_type="user",
            scope_id="default",
            importance=70,
        )
        assert result["status"] == "created", result
        assert result["scope"] == "user:default"
        memory_id = result["memory_id"]

        found = await search.execute(
            query="prefers dark mode",
            user_id="default",
        )
        assert found["status"] == "ok", found
        assert found["count"] >= 1, found
        matching = [
            r for r in found["results"]
            if r["memory_id"] == memory_id
        ]
        assert matching, f"saved memory not found: {found['results']}"
        assert "dark mode" in matching[0]["content"].lower()
        # The formatted block is non-empty and contains the memory.
        assert "[RETRIEVED MEMORY" in found["formatted"]
        assert "dark mode" in found["formatted"].lower()


async def test_project_scope_isolation() -> None:
    """Memories in one project are not visible to another project.

    Uses ``query="project"`` -- a token that appears in BOTH
    contents -- so that FTS relevance alone would return both
    entries. The scope filter is what must exclude the other
    project's entry. This is the strongest form of the isolation
    test: it fails if EITHER the FTS pipeline OR the scope filter
    is broken in a way that leaks cross-project data.
    """
    async with _Harness() as h:
        save = h.save_tool()
        search = h.search_tool()

        # Save in project alpha.
        await save.execute(
            content="The alpha project uses PostgreSQL 16",
            memory_type="decision",
            scope_type="project",
            scope_id="alpha",
            importance=80,
        )
        # Save in project beta.
        await save.execute(
            content="The beta project uses SQLite 3.45",
            memory_type="decision",
            scope_type="project",
            scope_id="beta",
            importance=80,
        )

        # Search from alpha's context. The query token "project"
        # matches BOTH stored entries via FTS, so any result
        # containing "SQLite" would indicate a scope-isolation
        # failure.
        alpha_results = await search.execute(
            query="project",
            project_id="alpha",
        )
        assert alpha_results["status"] == "ok"
        alpha_contents = [
            r["content"] for r in alpha_results["results"]
        ]
        assert any("PostgreSQL" in c for c in alpha_contents), (
            f"alpha project should see its own memory, "
            f"got: {alpha_contents}"
        )
        assert not any("SQLite" in c for c in alpha_contents), (
            f"alpha project must NOT see beta's memory, "
            f"got: {alpha_contents}"
        )

        # Search from beta's context. Symmetric check.
        beta_results = await search.execute(
            query="project",
            project_id="beta",
        )
        beta_contents = [
            r["content"] for r in beta_results["results"]
        ]
        assert any("SQLite" in c for c in beta_contents)
        assert not any("PostgreSQL" in c for c in beta_contents)


async def test_duplicate_is_skipped() -> None:
    """Saving identical content twice produces SKIP, not CREATE."""
    async with _Harness() as h:
        save = h.save_tool()

        content = "The API key lives in .env.production only"
        first = await save.execute(
            content=content,
            memory_type="semantic",
            scope_type="project",
            scope_id="acme",
        )
        assert first["status"] == "created", first

        second = await save.execute(
            content=content,
            memory_type="semantic",
            scope_type="project",
            scope_id="acme",
        )
        assert second["status"] == "skipped", (
            f"expected 'skipped' for duplicate, got {second}"
        )
        assert second["existing_id"] == first["memory_id"]


async def test_supersede_with_replacement_marker() -> None:
    """A superseding statement archives the old entry and creates the new one."""
    async with _Harness() as h:
        save = h.save_tool()

        old = await save.execute(
            content="The team uses Python 3.10 for all backend services",
            memory_type="decision",
            scope_type="project",
            scope_id="acme",
            tags=["python"],
            importance=70,
        )
        assert old["status"] == "created", old

        new = await save.execute(
            content=(
                "The team no longer uses Python 3.10; we switched "
                "to Python 3.12 for all backend services"
            ),
            memory_type="decision",
            scope_type="project",
            scope_id="acme",
            tags=["python"],
            importance=70,
        )
        # Either SUPERSEDE (replacement detected) or CREATE (not
        # detected -- then both stay active). Both are valid Phase-1
        # outcomes; we assert SUPERSEDE because the marker is explicit.
        assert new["status"] == "superseded", (
            f"expected 'superseded' for explicit replacement marker, "
            f"got {new}"
        )
        assert new["replaced_id"] == old["memory_id"]


async def test_read_only_mode_rejects_save() -> None:
    """READ_ONLY mode rejects every memory_save with mode_disabled code."""
    async with _Harness(mode=MemoryMode.READ_ONLY) as h:
        save = h.save_tool()
        result = await save.execute(
            content="Should not be saved",
            memory_type="semantic",
            scope_type="project",
            scope_id="acme",
        )
        assert result["status"] == "error", result
        assert result["code"] == "mode_disabled"


async def test_disabled_mode_rejects_search() -> None:
    """DISABLED mode rejects every memory_search with mode_disabled code."""
    async with _Harness(mode=MemoryMode.DISABLED) as h:
        search = h.search_tool()
        result = await search.execute(
            query="anything",
            project_id="acme",
        )
        assert result["status"] == "error", result
        assert result["code"] == "mode_disabled"


async def test_mark_used_populates_last_used_at() -> None:
    """After a search, the entry's last_used_at is populated in the DB.

    ``mark_used`` updates the DATABASE row, not the in-memory
    ``MemoryEntry`` objects returned by the same search call -- the
    search result reflects the state observed BEFORE the mark
    fired. A SECOND search is therefore required to observe the
    updated value; it proves that ``mark_used`` actually reached
    the DB (not just the in-memory list).

    This is the correct test for the port contract:
      * call 1 -- verifies ``mark_used`` fires without error,
      * call 2 -- verifies the DB row was actually updated and the
                  updated value round-trips back to the caller.
    """
    async with _Harness() as h:
        save = h.save_tool()
        search = h.search_tool()

        created = await save.execute(
            content="User timezone is Europe/Berlin",
            memory_type="preference",
            scope_type="user",
            scope_id="default",
        )
        assert created["status"] == "created"

        # First search: fires mark_used, but its result reflects the
        # pre-mark DB state -- ``last_used_at`` is still None on the
        # in-memory copy that the return dict is built from.
        first = await search.execute(
            query="timezone",
            user_id="default",
        )
        assert first["status"] == "ok"
        assert first["count"] >= 1

        # Second search: the DB row now carries the ``last_used_at``
        # written by the first search's mark_used call. The result
        # dict therefore reflects the populated timestamp.
        second = await search.execute(
            query="timezone",
            user_id="default",
        )
        assert second["status"] == "ok"
        matching = [
            r for r in second["results"]
            if r["memory_id"] == created["memory_id"]
        ]
        assert matching, (
            f"second search did not return the saved entry: "
            f"{second['results']}"
        )
        assert matching[0]["last_used_at"] is not None, (
            "second search must show the last_used_at populated "
            "by the first search's mark_used call"
        )


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------


async def _main() -> int:
    tests = [
        test_save_and_retrieve_user_scope,
        test_project_scope_isolation,
        test_duplicate_is_skipped,
        test_supersede_with_replacement_marker,
        test_read_only_mode_rejects_save,
        test_disabled_mode_rejects_search,
        test_mark_used_populates_last_used_at,
    ]
    failures = 0
    for fn in tests:
        name = fn.__name__
        try:
            await fn()
            print(f"PASS  {name}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL  {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERROR {name}: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()

    print()
    if failures:
        print(f"=== {failures} test(s) failed ===")
        return 1
    print(f"=== all {len(tests)} smoke tests passed ===")
    return 0


if __name__ == "__main__":
    _sys_bootstrap.exit(asyncio.run(_main()))
