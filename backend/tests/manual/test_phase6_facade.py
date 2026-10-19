# backend/tests/manual/test_phase6_facade.py
"""
Verify the facade fixes:
  1. Default managers are used (shared with tools).
  2. `_require_token` is async and awaits `_try_refresh_token`.
  3. `create_workspace` accepts `auth_token` in the manager call.
"""

from __future__ import annotations

import asyncio
import inspect
import sys


async def main() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"[ OK ] {label}")
        else:
            failures.append(label)
            print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))

    from omweb.services.source_control_facade import SourceControlFacade

    # --- 1. Managers are shared with tools -----------------------
    from integrations.source_control.git.workspace import (
        get_default_workspace_manager,
    )
    from integrations.source_control.git.diff import get_default_diff_engine
    from integrations.source_control.git.push import get_default_push_manager

    facade = SourceControlFacade()

    check(
        "facade.workspace_manager is default",
        facade.workspace_manager is get_default_workspace_manager(),
    )
    check(
        "facade.diff_engine is default",
        facade.diff_engine is get_default_diff_engine(),
    )
    check(
        "facade.push_manager is default",
        facade.push_manager is get_default_push_manager(),
    )

    # --- 2. _require_token is async ------------------------------
    sig = inspect.signature(SourceControlFacade._require_token)
    check(
        "_require_token is async",
        inspect.iscoroutinefunction(SourceControlFacade._require_token),
        f"signature: {sig}",
    )

    sig_refresh = inspect.signature(SourceControlFacade._try_refresh_token)
    check(
        "_try_refresh_token is async",
        inspect.iscoroutinefunction(SourceControlFacade._try_refresh_token),
        f"signature: {sig_refresh}",
    )

    # --- 3. create_workspace accepts auth_token -----------------
    sig_cw = inspect.signature(facade.workspace_manager.create_workspace)
    check(
        "manager.create_workspace accepts auth_token",
        "auth_token" in sig_cw.parameters,
    )

    # --- 4. _try_refresh_token returns None on failure ----------
    # Fake a connection record and call the helper. Since the
    # default token service has no stored token for this account,
    # `refresh` returns None (or raises), and the helper must
    # return None without raising.
    from omweb.services.source_control_facade import _ConnectionRecord
    from datetime import datetime, timezone

    conn = _ConnectionRecord(
        provider="github",
        account_id="phase6-nonexistent",
        base_url="default",
        username="tester",
        connected_at=datetime.now(timezone.utc),
    )
    from integrations.source_control.domain.enums import ProviderId

    try:
        result = await facade._try_refresh_token(ProviderId.GITHUB, conn)
        check(
            "_try_refresh_token returns None on missing token",
            result is None,
            f"got {result!r}",
        )
    except Exception as exc:
        check(
            "_try_refresh_token returns None on missing token",
            False,
            f"raised {type(exc).__name__}: {exc}",
        )

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print("PHASE 6 (FACADE) ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
