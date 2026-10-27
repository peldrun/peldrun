# scripts/version_manager.py
"""
Version Manager for PELDRUN Monorepo.

Manages four independent SemVer version streams:
    - app       : the whole application (top-level VERSION.json)
    - omweb     : backend/omweb subsystem
    - peldrun   : backend/peldrun core engine
    - frontend  : frontend/ Next.js app

Design:
    - Each component owns a VERSION.json file inside its own tree.
    - The top-level VERSION.json aggregates all components plus the
      app version.
    - Change detection uses `git diff` against the last released
      commit recorded in each component's VERSION.json.
    - The app version is auto-bumped (patch) whenever any component
      version changes, so the app version always reflects the state
      of the monorepo.

Usage:
    python scripts/version_manager.py show
    python scripts/version_manager.py detect
    python scripts/version_manager.py bump <component> <major|minor|patch>
    python scripts/version_manager.py release [--app-major|--app-minor]
    python scripts/version_manager.py verify
    python scripts/version_manager.py sync-app
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Paths (all resolved from this script's location)
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent

APP_VERSION_FILE = REPO_ROOT / "VERSION.json"
COMPONENT_PATHS: Dict[str, Path] = {
    "omweb": REPO_ROOT / "backend" / "omweb",
    "peldrun": REPO_ROOT / "backend" / "peldrun",
    "frontend": REPO_ROOT / "frontend",
}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Version:
    """SemVer version with bump operations."""

    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, text: str) -> "Version":
        text = text.strip().lstrip("v")
        parts = text.split(".")
        if len(parts) != 3:
            raise ValueError(f"Invalid SemVer: {text!r}")
        return cls(int(parts[0]), int(parts[1]), int(parts[2]))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    def bump(self, kind: str) -> "Version":
        if kind == "major":
            return Version(self.major + 1, 0, 0)
        if kind == "minor":
            return Version(self.major, self.minor + 1, 0)
        if kind == "patch":
            return Version(self.major, self.minor, self.patch + 1)
        raise ValueError(f"Unknown bump kind: {kind!r}")


# ---------------------------------------------------------------------------
# Version file I/O
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing version file: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def read_app_version() -> Dict[str, Any]:
    return _read_json(APP_VERSION_FILE)


def read_component_version(name: str) -> Dict[str, Any]:
    if name not in COMPONENT_PATHS:
        raise ValueError(f"Unknown component: {name!r}")
    return _read_json(COMPONENT_PATHS[name] / "VERSION.json")


def write_component_version(name: str, data: Dict[str, Any]) -> None:
    if name not in COMPONENT_PATHS:
        raise ValueError(f"Unknown component: {name!r}")
    _write_json(COMPONENT_PATHS[name] / "VERSION.json", data)


def write_app_version(data: Dict[str, Any]) -> None:
    _write_json(APP_VERSION_FILE, data)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _current_commit() -> str:
    try:
        return _git("rev-parse", "--short", "HEAD")
    except RuntimeError:
        return "unknown"


def _changed_files(component: str, since_commit: str) -> List[str]:
    """Return files under the component path changed since `since_commit`."""
    if component not in COMPONENT_PATHS:
        return []
    base = COMPONENT_PATHS[component].relative_to(REPO_ROOT)
    try:
        out = _git("diff", "--name-only", f"{since_commit}..HEAD", "--", str(base))
    except RuntimeError:
        return []
    return [line for line in out.splitlines() if line.strip()]


def _dirty_files(component: str) -> List[str]:
    """Return currently-uncommitted files under the component path."""
    if component not in COMPONENT_PATHS:
        return []
    base = COMPONENT_PATHS[component].relative_to(REPO_ROOT)
    try:
        out = _git("status", "--porcelain", "--", str(base))
    except RuntimeError:
        return []
    return [line[3:].strip() for line in out.splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_show(_args: argparse.Namespace) -> int:
    app = read_app_version()
    print(f"App:    {app['version']}")
    print(f"Released: {app.get('released_at', 'never')}")
    print(f"Commit:   {app.get('released_commit', 'unknown')}")
    print()
    print("Components:")
    for name in COMPONENT_PATHS:
        try:
            comp = read_component_version(name)
            print(
                f"  {name:10s} {comp['version']:12s} "
                f"(since {comp.get('released_commit', 'unknown')})"
            )
        except FileNotFoundError:
            print(f"  {name:10s} (missing)")
    return 0


def cmd_detect(_args: argparse.Namespace) -> int:
    any_change = False
    print("Change detection (vs each component's last release):")
    for name in COMPONENT_PATHS:
        try:
            comp = read_component_version(name)
        except FileNotFoundError:
            print(f"  [{name}] VERSION.json missing")
            continue

        since = comp.get("released_commit", "")
        if since and since != "unknown":
            committed = _changed_files(name, since)
        else:
            committed = []
        dirty = _dirty_files(name)
        total = len(committed) + len(dirty)

        status = "CHANGED" if total else "clean"
        print(f"  [{name:10s}] {status:8s} {total} file(s)")
        for f in (committed + dirty)[:5]:
            print(f"      - {f}")
        if total > 5:
            print(f"      ... and {total - 5} more")
        if total:
            any_change = True

    print()
    if not any_change:
        print("No component changes detected since last release.")
    else:
        print("Recommendation: run `bump <component> patch` or `release`.")
    return 0


def cmd_bump(args: argparse.Namespace) -> int:
    comp = args.component
    kind = args.kind

    data = read_component_version(comp)
    old = Version.parse(data["version"])
    new = old.bump(kind)
    data["version"] = str(new)
    data["released_at"] = _now()
    data["released_commit"] = _current_commit()

    history = data.setdefault("history", [])
    history.insert(0, {
        "version": str(new),
        "at": data["released_at"],
        "kind": kind,
        "summary": args.summary or f"bump {kind}",
    })
    write_component_version(comp, data)

    print(f"✅ {comp}: {old} → {new}")

    # Auto-bump app patch (unless explicitly disabled)
    if not args.no_app_bump:
        app = read_app_version()
        old_app = Version.parse(app["version"])
        new_app = old_app.bump("patch")
        app["version"] = str(new_app)
        app["released_at"] = _now()
        app["released_commit"] = _current_commit()
        app.setdefault("history", []).insert(0, {
            "version": str(new_app),
            "at": app["released_at"],
            "summary": f"{comp} bumped to {new}",
        })
        # Sync components map
        app.setdefault("components", {})
        for name in COMPONENT_PATHS:
            try:
                c = read_component_version(name)
                app["components"][name] = {
                    "version": c["version"],
                    "commit": c.get("released_commit", "unknown"),
                }
            except FileNotFoundError:
                pass
        write_app_version(app)
        print(f"✅ app:   {old_app} → {new_app}")

    return 0


def cmd_release(args: argparse.Namespace) -> int:
    """
    Detect component changes and bump each one (patch), then bump app.

    App bump level: --app-major / --app-minor / --app-patch (default: patch)
    Components are always bumped at patch level; use `bump` for
    fine-grained control.
    """
    changed_components: List[str] = []

    for name in COMPONENT_PATHS:
        try:
            comp = read_component_version(name)
        except FileNotFoundError:
            continue
        since = comp.get("released_commit", "")
        committed = _changed_files(name, since) if since and since != "unknown" else []
        dirty = _dirty_files(name)
        if committed or dirty:
            changed_components.append(name)

    if not changed_components:
        print("No component changes detected.")

    for name in changed_components:
        data = read_component_version(name)
        old = Version.parse(data["version"])
        new = old.bump("patch")
        data["version"] = str(new)
        data["released_at"] = _now()
        data["released_commit"] = _current_commit()
        data.setdefault("history", []).insert(0, {
            "version": str(new),
            "at": data["released_at"],
            "kind": "patch",
            "summary": f"release: {name}",
        })
        write_component_version(name, data)
        print(f"✅ {name}: {old} → {new}")

    # App bump
    app_kind = "patch"
    if args.app_major:
        app_kind = "major"
    elif args.app_minor:
        app_kind = "minor"

    app = read_app_version()
    old_app = Version.parse(app["version"])
    new_app = old_app.bump(app_kind)
    app["version"] = str(new_app)
    app["released_at"] = _now()
    app["released_commit"] = _current_commit()

    app.setdefault("components", {})
    for name in COMPONENT_PATHS:
        try:
            c = read_component_version(name)
            app["components"][name] = {
                "version": c["version"],
                "commit": c.get("released_commit", "unknown"),
            }
        except FileNotFoundError:
            pass

    app.setdefault("history", []).insert(0, {
        "version": str(new_app),
        "at": app["released_at"],
        "kind": app_kind,
        "summary": (
            f"components bumped: {', '.join(changed_components)}"
            if changed_components
            else "app-only bump"
        ),
    })
    write_app_version(app)
    print(f"✅ app:   {old_app} → {new_app} ({app_kind})")

    if changed_components:
        print(f"\nComponents released: {', '.join(changed_components)}")
    return 0


def cmd_verify(_args: argparse.Namespace) -> int:
    """
    Verify that the app's components map matches the actual component
    VERSION.json files.
    """
    app = read_app_version()
    errors: List[str] = []
    app_components = app.get("components", {})

    for name in COMPONENT_PATHS:
        try:
            comp = read_component_version(name)
        except FileNotFoundError:
            errors.append(f"{name}: VERSION.json missing")
            continue
        recorded = app_components.get(name, {}).get("version")
        actual = comp["version"]
        if recorded != actual:
            errors.append(
                f"{name}: app says {recorded!r}, file says {actual!r}"
            )

    if errors:
        print("❌ Version mismatch:")
        for e in errors:
            print(f"  - {e}")
        print("\nRun `sync-app` to fix the app's components map.")
        return 1

    print("✅ All versions are in sync.")
    return 0


def cmd_sync_app(_args: argparse.Namespace) -> int:
    """Resync the app's components map from each component's VERSION.json."""
    app = read_app_version()
    app.setdefault("components", {})
    for name in COMPONENT_PATHS:
        try:
            c = read_component_version(name)
            app["components"][name] = {
                "version": c["version"],
                "commit": c.get("released_commit", "unknown"),
            }
            print(f"  synced {name}: {c['version']}")
        except FileNotFoundError:
            print(f"  skipped {name}: VERSION.json missing")
    write_app_version(app)
    print("✅ App components map updated.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PELDRUN version manager",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", help="Show current versions")
    p_show.set_defaults(func=cmd_show)

    p_detect = sub.add_parser("detect", help="Detect component changes")
    p_detect.set_defaults(func=cmd_detect)

    p_bump = sub.add_parser("bump", help="Bump a component version")
    p_bump.add_argument("component", choices=list(COMPONENT_PATHS))
    p_bump.add_argument("kind", choices=["major", "minor", "patch"])
    p_bump.add_argument("--summary", default=None)
    p_bump.add_argument("--no-app-bump", action="store_true",
                        help="Do not auto-bump the app version")
    p_bump.set_defaults(func=cmd_bump)

    p_rel = sub.add_parser("release", help="Detect + bump all changed components")
    group = p_rel.add_mutually_exclusive_group()
    group.add_argument("--app-major", action="store_true")
    group.add_argument("--app-minor", action="store_true")
    p_rel.set_defaults(func=cmd_release)

    p_verify = sub.add_parser("verify", help="Verify versions in sync")
    p_verify.set_defaults(func=cmd_verify)

    p_sync = sub.add_parser("sync-app", help="Resync app components map")
    p_sync.set_defaults(func=cmd_sync_app)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:
        print(f"❌ {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
