# backend/_debug_memory.py
# TEMPORARY diagnostic script.

import asyncio
import sqlite3
from pathlib import Path

from peldrun.storage_paths import get_storage_root
from peldrun.memory.services.router import classify_route
from peldrun.memory.services.context_builder import build_memory_context
from peldrun.memory.providers.sqlite_fts import SqliteFtsProvider
from peldrun.memory.domain import (
    default_settings,
    resolve_scope_refs,
    MemoryScope,
)


async def main():
    # ----- Step 1: router -----
    query = "What is my name and what do I do?"
    route = classify_route(query)
    print(f"[ROUTER] query={query!r}")
    print(f"[ROUTER] decision={route.decision.value!r} reason={route.reason!r}")

    # ----- Step 2: real DB path via get_storage_root() -----
    storage_root = get_storage_root()
    db_path = storage_root / "memory.db"
    print(f"[DB] storage_root={storage_root}")
    print(f"[DB] path={db_path}")
    print(f"[DB] exists={db_path.exists()}")
    if db_path.exists():
        print(f"[DB] size_bytes={db_path.stat().st_size}")

    # ----- Step 3: raw SQL inspection -----
    if db_path.exists():
        conn = sqlite3.connect(str(db_path), timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT memory_id, scope_type, scope_id, content, status "
                "FROM memories ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            print(f"[DB] total_visible_rows={len(rows)}")
            for r in rows:
                print(
                    f"[DB]   {r['memory_id']} scope="
                    f"{r['scope_type']}:{r['scope_id']} "
                    f"status={r['status']} "
                    f"content={r['content'][:60]!r}"
                )
        except sqlite3.Error as exc:
            print(f"[DB] SQL error: {exc}")
        finally:
            conn.close()

    # ----- Step 4: scope resolution (mimics chat context) -----
    scope_refs = resolve_scope_refs(
        chat_id="chat_mwdy7a8j_22swgz",  # from your failed log
        project_id="default_project",
        user_id="default",
    )
    print(f"[SCOPES] resolved={scope_refs}")

    # ----- Step 5: provider + context builder -----
    if not db_path.exists():
        print("[PROVIDER] DB missing — cannot continue.")
        return

    provider = SqliteFtsProvider(db_path=db_path)
    await provider.open()
    is_open = await provider.is_open()
    print(f"[PROVIDER] is_open={is_open}")

    settings = default_settings()
    print(
        f"[SETTINGS] mode={settings.mode.value} "
        f"provider_id={settings.provider_id} "
        f"default_limit={settings.default_limit} "
        f"max_tokens={settings.token_budget.max_tokens}"
    )

    # Try user:default explicitly first
    print()
    print("=== Context build with EXPLICIT user:default scope ===")
    ctx_user = await build_memory_context(
        query=query,
        scope_refs=["user:default"],
        route=route,
        provider=provider,
        settings=settings,
    )
    print(
        f"[CONTEXT-user] entries={len(ctx_user.memories)} "
        f"used_tokens={ctx_user.used_tokens} "
        f"router={ctx_user.router_decision}"
    )
    for m in ctx_user.memories:
        print(f"[CONTEXT-user]   - {m.content!r}")

    # Now with the scope_refs that a chat would actually use
    print()
    print("=== Context build with CHAT-RESOLVED scope_refs ===")
    ctx_chat = await build_memory_context(
        query=query,
        scope_refs=scope_refs,
        route=route,
        provider=provider,
        settings=settings,
    )
    print(
        f"[CONTEXT-chat] entries={len(ctx_chat.memories)} "
        f"used_tokens={ctx_chat.used_tokens}"
    )
    for m in ctx_chat.memories:
        print(f"[CONTEXT-chat]   - {m.content!r}")

    await provider.close()


asyncio.run(main())
