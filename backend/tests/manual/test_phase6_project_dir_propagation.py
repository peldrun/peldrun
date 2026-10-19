# backend/tests/manual/test_phase6_project_dir_propagation.py
"""
Verify that `project_dir` reaches `create_workspace` through the full
chain: `resolve_tool_runtime` -> `_invoke_adapter` -> `SourceControlTool`.

This test does not touch the network, does not require credentials, and
does not modify any state. It patches `SourceControlTool.aexecute` to
capture the kwargs the dispatcher forwards, then asserts that the exact
`project_dir` we provided is present in `context_metadata` — which is
what `CreateWorkspaceTool` reads to decide where to place the Git
workspace.

If this test fails, the entire Phase 1 fix (chat-scoped workspaces) is
silently broken for the agent workflow, regardless of what the unit
tests say.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import uuid
from unittest.mock import patch


async def main() -> int:
    failures: list[str] = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        if cond:
            print(f"[ OK ] {label}")
        else:
            failures.append(label)
            print(f"[FAIL] {label}" + (f" — {detail}" if detail else ""))

    # --- Setup: temp project_dir under storage/ -----------------
    from integrations.source_control.git.path_policy import (
        get_default_path_policy,
        reset_default_path_policy,
    )
    reset_default_path_policy()
    policy = get_default_path_policy()
    if not policy.extra_roots:
        check("storage root available", False, "no extra_roots")
        return 1
    storage_root = policy.extra_roots[0]

    chat_id = f"test_{uuid.uuid4().hex[:8]}"
    project_dir = storage_root / "chats" / chat_id / "files"
    project_dir.mkdir(parents=True, exist_ok=True)

    # --- Patch SourceControlTool.aexecute to capture kwargs -----
    from peldrun.tools.builtins.source_control_tool import SourceControlTool
    from peldrun.tools.contract import ToolResult

    captured: dict = {}

    async def fake_aexecute(self, **kwargs):
        captured.update(kwargs)
        return ToolResult(output={"ok": True}, exit_code=0, is_error=False)

    # --- Exercise the full chain -------------------------------
    from omweb.adapters.core_adapter_parts.resolve_tool_runtime import (
        resolve_tool_runtime,
    )

    run_id = str(uuid.uuid4())
    try:
        with patch.object(SourceControlTool, "aexecute", fake_aexecute):
            adapter = resolve_tool_runtime(
                tool_id="source_control",
                tool_meta={"id": "source_control"},
                workspace_root=project_dir,
                run_id=run_id,
                job_id="test-job",
                chat_id=chat_id,
                project_id="default_project",
            )

            # Discover the adapter's invocation interface. Depending
            # on the build, it may expose `.aexecute`, `.execute`,
            # `__call__`, or `.executor`.
            invoke = None
            for attr in ("aexecute", "execute"):
                candidate = getattr(adapter, attr, None)
                if candidate and callable(candidate):
                    invoke = candidate
                    break
            if invoke is None and hasattr(adapter, "executor"):
                invoke = adapter.executor
            if invoke is None and callable(adapter):
                invoke = adapter

            if invoke is None:
                check(
                    "adapter invoke interface",
                    False,
                    f"attributes: "
                    f"{[a for a in dir(adapter) if not a.startswith('__')]}",
                )
                return 1

            try:
                await invoke(
                    action="create_workspace",
                    repository_id="owner/repo",
                )
            except Exception as exc:
                check(
                    "adapter invocation",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
                return 1

        # --- Assertions ---------------------------------------
        check("adapter reached aexecute", bool(captured), f"kwargs={captured!r}")

        ctx = captured.get("context_metadata", {})
        check("context_metadata present", bool(ctx), f"got {ctx!r}")
        check(
            "context_metadata.project_dir",
            ctx.get("project_dir") == str(project_dir.resolve()),
            f"got {ctx.get('project_dir')!r}, "
            f"expected {str(project_dir.resolve())!r}",
        )
        check(
            "context_metadata.chat_id",
            ctx.get("chat_id") == chat_id,
            f"got {ctx.get('chat_id')!r}",
        )
        check(
            "context_metadata.project_id",
            ctx.get("project_id") == "default_project",
            f"got {ctx.get('project_id')!r}",
        )
        check(
            "context_metadata.job_id",
            ctx.get("job_id") == "test-job",
            f"got {ctx.get('job_id')!r}",
        )
        check(
            "top-level run_id forwarded",
            captured.get("run_id") == run_id,
            f"got {captured.get('run_id')!r}",
        )
        check(
            "action preserved",
            captured.get("action") == "create_workspace",
            f"got {captured.get('action')!r}",
        )
        check(
            "repository_id preserved",
            captured.get("repository_id") == "owner/repo",
            f"got {captured.get('repository_id')!r}",
        )
    finally:
        shutil.rmtree(project_dir.parent, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILURE(S)")
        return 1
    print("PHASE 6 (PROJECT_DIR PROPAGATION) ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
