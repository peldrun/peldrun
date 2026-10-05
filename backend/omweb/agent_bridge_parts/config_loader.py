"""
config.toml loader.

Reads the active engine's config.toml file directly from disk,
trying several candidate locations in priority order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from omweb.engine_resolver import resolve_active_engine_path


def read_active_toml_config() -> Dict[str, Any]:
    """Read active config.toml from engine path directly from disk.

    Candidate search order (first existing file wins):
        1. <engine_path>/config/config.toml
        2. <engine_path>/config.toml
        3. <original_module_parent>/config.toml   (project root)
        4. D:\\AI\\peldrun\\config\\config.toml
        5. D:\\AI\\peldrun-core\\config.toml

    Parsers are tried in this order: tomllib (Python 3.11+),
    then tomli, then toml.

    Returns:
        The parsed config as a dict, or an empty dict if no file was
        found or parsing failed.
    """
    engine_path = resolve_active_engine_path()

    # NOTE: In the original monolithic agent_bridge.py, the third candidate
    # was computed as ``Path(__file__).resolve().parent.parent / "config.toml"``.
    # Because this module now lives one level deeper (inside agent_bridge_parts/),
    # we add one extra ``.parent`` so the resolved path is IDENTICAL to the
    # original (i.e. it still points to the same project root).
    _original_parent_parent = Path(__file__).resolve().parent.parent.parent

    candidates = [
        engine_path / "config" / "config.toml",
        engine_path / "config.toml",
        _original_parent_parent / "config.toml",
        Path(r"D:\AI\peldrun\config\config.toml"),
        Path(r"D:\AI\peldrun-core\config.toml"),
    ]

    target_file = None
    for cand in candidates:
        if cand.is_file():
            target_file = cand
            break

    if not target_file:
        return {}

    try:
        try:
            import tomllib
            with open(target_file, "rb") as f:
                return tomllib.load(f)
        except ImportError:
            try:
                import tomli
                with open(target_file, "rb") as f:
                    return tomli.load(f)
            except ImportError:
                import toml
                with open(target_file, "r", encoding="utf-8") as f:
                    return toml.load(f)
    except Exception as e:
        print(f"[BRIDGE WARNING] Could not parse config.toml: {e}")
        return {}