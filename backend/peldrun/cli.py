"""
PELDRUN Core Standalone Command Line Interface (CLI).
Provides developer entrypoints for executing tasks, inspecting registered tools,
validating LLM provider connectivity, and querying package metadata.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import List, Optional, Sequence

import peldrun
from peldrun.agents import get_agent
from peldrun.events.emitter import EventEmitter
from peldrun.events.schema import AgentEvent, EventType
from peldrun.llm.client import LLMConfig
from peldrun.llm.providers import BaseLLMProvider, get_provider
from peldrun.memory import MemoryManager
from peldrun.tools.builtins.file_ops import FileOpsTool
from peldrun.tools.builtins.human_input import HumanInputTool
from peldrun.tools.builtins.shell_exec import ShellExecTool
from peldrun.tools.builtins.web_search import WebSearchTool
from peldrun.tools.registry import ToolRegistry

logger = logging.getLogger("peldrun.cli")


def _build_provider(
    provider_name: str,
    model_name: str,
    api_base: Optional[str] = None,
) -> BaseLLMProvider:
    """Instantiate appropriate LLM provider from CLI arguments."""
    p_name = provider_name.lower().strip()
    if p_name == "lmstudio":
        base_url = api_base or "http://localhost:1234/v1"
        return get_provider("lmstudio", api_base=base_url, model=model_name)
    elif p_name == "ollama":
        base_url = api_base or "http://localhost:11434/v1"
        return get_provider("ollama", api_base=base_url, model=model_name)
    else:
        cfg = LLMConfig(
            model=model_name,
            api_base=api_base or "http://localhost:1234/v1",
        )
        return get_provider("openai_compat", config=cfg)


def _build_tool_registry(workspace_root: str) -> ToolRegistry:
    """Populate default registry with all core builtin tools."""
    registry = ToolRegistry(workspace_root=workspace_root)
    registry.register(FileOpsTool(workspace_root=workspace_root))
    registry.register(ShellExecTool(workspace_root=workspace_root))
    registry.register(WebSearchTool(workspace_root=workspace_root))
    registry.register(HumanInputTool())
    return registry


async def _async_run_task(
    task: str,
    agent_type: str,
    model_name: str,
    provider_name: str,
    api_base: Optional[str],
    workspace_root: str,
    max_steps: int,
    verbose: bool,
) -> int:
    """Asynchronously assemble and run agent workflow."""
    ws_path = Path(workspace_root).resolve()
    ws_path.mkdir(parents=True, exist_ok=True)

    provider = _build_provider(provider_name, model_name, api_base)
    tool_registry = _build_tool_registry(str(ws_path))
    memory_manager = MemoryManager(
        workspace_root=str(ws_path),
        system_prompt="You are PELDRUN Core autonomous execution agent.",
    )
    await memory_manager.ainitialize()

    emitter = EventEmitter()

    if verbose:
        async def _cli_event_logger(event: AgentEvent) -> None:
            etype = event.type.value if hasattr(event.type, "value") else str(event.type)
            if etype == "thought":
                print(f"\n[THOUGHT] {event.data.get('thought', '')}")
            elif etype == "tool_call":
                tname = event.data.get("tool_name", "")
                args = event.data.get("arguments", {})
                print(f"[TOOL_CALL] {tname} -> {args}")
            elif etype == "observation":
                tname = event.data.get("tool_name", "")
                output = str(event.data.get("output", ""))
                snippet = output[:160] + "..." if len(output) > 160 else output
                print(f"[OBSERVATION] {tname} -> {snippet}")
            elif etype == "error":
                print(f"[ERROR] {event.data.get('error', '')}")

        emitter.subscribe_all(_cli_event_logger)

    try:
        agent = get_agent(
            agent_type=agent_type,
            llm_provider=provider,
            tool_registry=tool_registry,
            memory_manager=memory_manager,
            emitter=emitter,
        )
        if hasattr(agent, "config") and hasattr(agent.config, "max_steps"):
            agent.config.max_steps = max_steps

        print(f"[*] Starting PELDRUN Agent ({agent_type}) for task: '{task}'")
        state = await agent.arun(task)

        print("\n" + "=" * 60)
        print(f"Status:       {'COMPLETED' if state.is_completed else 'FAILED'}")
        print(f"Total Steps:  {state.step}")
        if state.deliverables:
            print(f"Deliverables: {', '.join(state.deliverables)}")
        if state.final_output:
            print(f"\nFinal Output:\n{state.final_output}")
        print("=" * 60)

        return 0 if state.is_completed else 1

    except Exception as ex:
        print(f"\n[FATAL] Execution failed: {ex}")
        logger.exception("CLI execution exception: %s", ex)
        return 1
    finally:
        await provider.close()


async def _async_check_connectivity(
    provider_name: str,
    model_name: str,
    api_base: Optional[str],
) -> int:
    """Validate connectivity with specified provider endpoint."""
    provider = _build_provider(provider_name, model_name, api_base)
    try:
        print(f"[*] Probing LLM provider '{provider_name}'...")
        is_healthy = await provider.check_health()
        if is_healthy:
            print(f"[OK] Connection established successfully with '{provider_name}'.")
            return 0
        else:
            print(f"[FAIL] Provider '{provider_name}' endpoint is unresponsive or unhealthy.")
            return 1
    except Exception as ex:
        print(f"[FAIL] Error connecting to '{provider_name}': {ex}")
        return 1
    finally:
        await provider.close()


def _cmd_tools(workspace_root: str) -> int:
    """List all registered tools."""
    registry = _build_tool_registry(workspace_root)
    tools = registry.list_tools()

    print(f"\nPELDRUN Core Registered Tools ({len(tools)}):")
    print("-" * 65)
    for name in sorted(tools):
        tool = registry.get(name)
        desc = (tool.description or "").replace("\n", " ").strip() if tool else ""
        print(f"  • {name:16} : {desc}")
    print("-" * 65 + "\n")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Main CLI entrypoint parsing arguments and dispatching commands."""
    parser = argparse.ArgumentParser(
        prog="peldrun",
        description="PELDRUN Core Autonomous Agent Orchestration CLI",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"peldrun-core v{peldrun.__version__}",
    )

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Execute an autonomous agent task")
    run_parser.add_argument("task", type=str, help="Task directive for the agent")
    run_parser.add_argument(
        "-a", "--agent",
        type=str,
        default="react",
        choices=["react", "planning", "coding"],
        help="Agent variant to instantiate (default: react)",
    )
    run_parser.add_argument(
        "-m", "--model",
        type=str,
        default="local-model",
        help="Model identifier (default: local-model)",
    )
    run_parser.add_argument(
        "-p", "--provider",
        type=str,
        default="openai_compat",
        choices=["openai_compat", "lmstudio", "ollama"],
        help="LLM provider gateway (default: openai_compat)",
    )
    run_parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="Custom base URL for the LLM endpoint",
    )
    run_parser.add_argument(
        "-w", "--workspace",
        type=str,
        default=".",
        help="Workspace root path (default: current directory)",
    )
    run_parser.add_argument(
        "-s", "--max-steps",
        type=int,
        default=15,
        help="Maximum step ceiling before termination (default: 15)",
    )
    run_parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Stream real-time thought and tool execution events to stdout",
    )

    # Command: tools
    tools_parser = subparsers.add_parser("tools", help="List all available builtin tools")
    tools_parser.add_argument(
        "-w", "--workspace",
        type=str,
        default=".",
        help="Workspace root path for schema generation",
    )

    # Command: check
    check_parser = subparsers.add_parser("check", help="Verify reachability of LLM provider")
    check_parser.add_argument(
        "-p", "--provider",
        type=str,
        default="openai_compat",
        choices=["openai_compat", "lmstudio", "ollama"],
        help="Provider to check (default: openai_compat)",
    )
    check_parser.add_argument(
        "-m", "--model",
        type=str,
        default="local-model",
        help="Target model name",
    )
    check_parser.add_argument(
        "--api-base",
        type=str,
        default=None,
        help="Custom base URL for the LLM endpoint",
    )

    # Command: version
    subparsers.add_parser("version", help="Print current peldrun-core package version")

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"peldrun-core v{peldrun.__version__}")
        return 0

    elif args.command == "tools":
        return _cmd_tools(workspace_root=args.workspace)

    elif args.command == "check":
        return asyncio.run(
            _async_check_connectivity(
                provider_name=args.provider,
                model_name=args.model,
                api_base=args.api_base,
            )
        )

    elif args.command == "run":
        return asyncio.run(
            _async_run_task(
                task=args.task,
                agent_type=args.agent,
                model_name=args.model,
                provider_name=args.provider,
                api_base=args.api_base,
                workspace_root=args.workspace,
                max_steps=args.max_steps,
                verbose=args.verbose,
            )
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())