"""
Programmatic factory for building test bundle zips in memory.

Keeps tests hermetic: no checked-in binary fixtures, no reliance on the
host filesystem's zip tooling.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any, Dict, Iterable, Optional, Tuple


def make_manifest(**overrides: Any) -> Dict[str, Any]:
    """Return a valid manifest dict, optionally overridden."""
    base: Dict[str, Any] = {
        "schema_version": "1.0.0",
        "id": "test_tool",
        "version": "1.0.0",
        "name": "Test Tool",
        "description": "A tool for testing.",
        "entry_point": "tool.py",
    }
    base.update(overrides)
    return base


def make_tool_source(class_name: str = "TestTool") -> str:
    """Return minimal but valid Python source for a SovereignBaseTool subclass."""
    return (
        "from omweb.tools.base import SovereignBaseTool\n"
        "\n"
        f"class {class_name}(SovereignBaseTool):\n"
        f'    name: str = "test_tool"\n'
        f'    description: str = "Test tool."\n'
        f'    category: str = "custom"\n'
        "\n"
        "    async def execute(self, **kwargs):\n"
        '        return {"output": "ok", "is_error": False, "exit_code": 0}\n'
    )


def build_zip(files: Iterable[Tuple[str, bytes]]) -> bytes:
    """
    Build a zip in memory from `(path, bytes)` pairs.

    Entries ending with '/' are treated as directory markers.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, data in files:
            zf.writestr(path, data)
    return buf.getvalue()


def make_valid_bundle(
    manifest_overrides: Optional[Dict[str, Any]] = None,
    extra_files: Optional[Dict[str, bytes]] = None,
    class_name: str = "TestTool",
) -> bytes:
    """
    Build a valid, minimal bundle.

    Produces:
        manifest.json
        tool.py
        (optional extra_files)
    """
    manifest = make_manifest(**(manifest_overrides or {}))
    files: list[Tuple[str, bytes]] = [
        ("manifest.json", json.dumps(manifest).encode("utf-8")),
        ("tool.py", make_tool_source(class_name).encode("utf-8")),
    ]
    if extra_files:
        for path, data in extra_files.items():
            files.append((path, data))
    return build_zip(files)


def make_bundle_with_requirements(
    requirements: str = "pypdf2>=3.0.0\n",
) -> bytes:
    """Build a bundle that includes a requirements.txt."""
    return make_valid_bundle(
        manifest_overrides={
            "id": "with_reqs",
            "name": "With Requirements",
            "entry_point": "tool.py",
        },
        extra_files={"requirements.txt": requirements.encode("utf-8")},
    )


def make_bundle_missing_manifest() -> bytes:
    """Build a bundle that omits manifest.json entirely."""
    return build_zip(
        [("tool.py", make_tool_source().encode("utf-8"))]
    )


def make_bundle_with_traversal() -> bytes:
    """Build a bundle with a `..` traversal segment in a member path."""
    manifest = make_manifest()
    return build_zip(
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", make_tool_source().encode("utf-8")),
            ("../evil.txt", b"malicious"),
        ]
    )


def make_bundle_with_absolute_path() -> bytes:
    """Build a bundle with an absolute path member."""
    manifest = make_manifest()
    return build_zip(
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", make_tool_source().encode("utf-8")),
            ("/etc/passwd", b"malicious"),
        ]
    )


def make_bundle_with_symlink() -> bytes:
    """
    Build a bundle containing a symlink entry.

    Zip stores symlinks as regular files whose external attributes carry
    the UNIX `S_IFLNK` bit. We reproduce that here.
    """
    manifest = make_manifest()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest).encode("utf-8"))
        zf.writestr("tool.py", make_tool_source().encode("utf-8"))

        # Create symlink entry with appropriate external attrs
        info = zipfile.ZipInfo("link_to_etc")
        info.external_attr = (0o120777 << 16)  # S_IFLNK | 0777
        zf.writestr(info, "/etc/passwd")

    return buf.getvalue()


def make_bundle_too_many_files(limit_plus: int = 5) -> bytes:
    """Build a bundle with more files than the MAX_FILE_COUNT limit."""
    manifest = make_manifest()
    files: list[Tuple[str, bytes]] = [
        ("manifest.json", json.dumps(manifest).encode("utf-8")),
        ("tool.py", make_tool_source().encode("utf-8")),
    ]
    for i in range(600):
        files.append((f"junk/file_{i}.txt", b"x"))
    return build_zip(files)


def make_bundle_with_forbidden_dir() -> bytes:
    """Build a bundle containing a forbidden directory like __pycache__."""
    manifest = make_manifest()
    return build_zip(
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", make_tool_source().encode("utf-8")),
            ("__pycache__/cached.pyc", b"junk"),
        ]
    )


def make_bundle_with_single_oversized_file(
    size_bytes: int = 6 * 1024 * 1024,
) -> bytes:
    """Build a bundle containing a single file larger than MAX_SINGLE_FILE_BYTES."""
    manifest = make_manifest()
    return build_zip(
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", make_tool_source().encode("utf-8")),
            ("big.bin", b"\x00" * size_bytes),
        ]
    )


def make_bundle_with_helpers() -> bytes:
    """Build a valid bundle that also includes a helper module."""
    manifest = make_manifest(id="with_helpers", name="With Helpers")
    return build_zip(
        [
            ("manifest.json", json.dumps(manifest).encode("utf-8")),
            ("tool.py", make_tool_source().encode("utf-8")),
            ("helpers/__init__.py", b""),
            ("helpers/util.py", b"def hello():\n    return 'world'\n"),
            ("README.md", b"# With Helpers\n"),
        ]
    )


__all__ = [
    "make_manifest",
    "make_tool_source",
    "build_zip",
    "make_valid_bundle",
    "make_bundle_with_requirements",
    "make_bundle_missing_manifest",
    "make_bundle_with_traversal",
    "make_bundle_with_absolute_path",
    "make_bundle_with_symlink",
    "make_bundle_too_many_files",
    "make_bundle_with_forbidden_dir",
    "make_bundle_with_single_oversized_file",
    "make_bundle_with_helpers",
]
