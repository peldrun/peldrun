# PELDRUN Provider Package Repository: The Complete Developer Guide

Welcome to the PELDRUN Provider Package ecosystem. This guide is designed for developers who want to create, build, test, and publish a new LLM provider for PELDRUN.

While PELDRUN runs the core agent logic, **Provider Packages** are standalone repositories. They contain the adapter code, the manifest, and the automation scripts required to package and distribute the provider.

This document focuses heavily on the **repository structure, the six core automation scripts, and the CI/CD lifecycle**. Every provider repository (Cloudflare, GigaChat, OpenAI, etc.) shares this exact structural DNA.

---

## Table of Contents
1. [Repository Architecture](#1-repository-architecture)
2. [Core Configuration Files](#2-core-configuration-files)
3. [Deep Dive: The 6 Automation Scripts](#3-deep-dive-the-6-automation-scripts)
   - [3.1 validate_manifest.py](#31-validate_manifestpy)
   - [3.2 update_version.py](#32-update_versionpy)
   - [3.3 changelog.py](#33-changelogpy)
   - [3.4 build_package.py](#34-build_packagepy)
   - [3.5 verify_package.py](#35-verify_packagepy)
   - [3.6 generate_catalog_entry.py](#36-generate_catalog_entrypy)
4. [CI/CD Workflows](#4-cicd-workflows)
5. [Step-by-Step Development Lifecycle](#5-step-by-step-development-lifecycle)

---

## 1. Repository Architecture

Every PELDRUN provider repository follows this exact layout. This ensures that GitHub Actions, the local build scripts, and the PELDRUN backend can reliably find and process the code.

```text
peldrun-provider-yourname/
│
├── .github/
│   └── workflows/
│       ├── validate.yml          # CI: Runs on PRs (tests & builds)
│       └── release.yml           # CI: Runs on tags (publishes release)
│
├── dist/                         # Build artifacts (ZIP, JSON) - GITIGNORED
│
├── scripts/                      # Automation scripts
│   ├── build_package.py
│   ├── changelog.py
│   ├── generate_catalog_entry.py
│   ├── update_version.py
│   ├── validate_manifest.py
│   └── verify_package.py
│
├── src/
│   └── yourname_peldrun/         # The Python package
│       ├── __init__.py
│       ├── adapter.py
│       └── py.typed
│
├── tests/
│   └── test_manifest.py          # Integrity tests
│
├── .gitignore
├── CHANGELOG.md
├── LICENSE
├── manifest.json
├── pyproject.toml
├── README.md
└── requirements.txt
```

---

## 2. Core Configuration Files

Before diving into the scripts, understand the inputs they operate on:

*   **`manifest.json`**: The core definition file. It declares the `id`, `transport`, `entry_point`, `permissions`, and `connection_fields`.
*   **`requirements.txt`**: The isolated dependencies for this provider. Must be aligned with PELDRUN's own backend pins to prevent pip resolver conflicts inside the isolated venv.
*   **`pyproject.toml`**: Used for local development (`pip install -e .`) and configuring tools like `pytest` and `ruff`.

---

## 3. Deep Dive: The 6 Automation Scripts

These scripts are the engine of the repository. They guarantee that every release is consistent, secure, and ready for the PELDRUN Store. They are entirely stdlib-only (no third-party dependencies required to run them).

### 3.1 `validate_manifest.py`
**Purpose:** Static analysis of `manifest.json`. It catches typos, missing fields, invalid semver, and unsafe permissions *before* you commit. If `peldrun` is installed locally, it also runs the actual `ProviderManifest` Pydantic model against the JSON.

```python
# scripts/validate_manifest.py
import json
import re
import sys
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _BASE_DIR / "manifest.json"

def validate(path: Path) -> bool:
    if not path.is_file():
        print(f"error: {path} not found")
        return False

    data = json.loads(path.read_text(encoding="utf-8"))

    # 1. Check required fields
    required = ["schema_version", "kind", "id", "name", "version", "transport", "permissions"]
    for field in required:
        if field not in data:
            print(f"[err] Missing required field: {field}")
            return False

    # 2. Validate provider ID pattern (lowercase, no spaces)
    if not re.match(r"^[a-z0-9][a-z0-9._-]*$", data["id"]):
        print(f"[err] Invalid ID '{data['id']}'. Must be lowercase alphanumeric.")
        return False

    # 3. Validate semver
    if not re.match(r"^\d+\.\d+\.\d+", data["version"]):
        print(f"[err] Invalid version '{data['version']}'. Must be SemVer.")
        return False

    # 4. Validate Entry Point (if native)
    if data.get("transport") in ("native", "custom"):
        entry_point = data.get("entry_point")
        if not entry_point or ":" not in entry_point:
            print("[err] Native transport requires 'entry_point' (module:ClassName)")
            return False

        module_path = entry_point.split(":")[0].replace(".", "/")
        possible_files = [
            _BASE_DIR / "src" / f"{module_path}.py",
            _BASE_DIR / "src" / module_path / "__init__.py"
        ]
        if not any(f.is_file() for f in possible_files):
            print(f"[err] entry_point module '{module_path}' does not resolve to a file in src/")
            return False

    # 5. Validate Permissions
    perms = data.get("permissions", {})
    if perms.get("workspace_access") is not False:
        print("[err] permissions.workspace_access must be false")
        return False
    if perms.get("shell_execute") is not False:
        print("[err] permissions.shell_execute must be false")
        return False

    print("[ok] manifest.json is valid.")
    return True

if __name__ == "__main__":
    sys.exit(0 if validate(_MANIFEST_PATH) else 1)
```

---

### 3.2 `update_version.py`
**Purpose:** The single source of truth for version bumping. You **never** manually edit version numbers. This script updates `manifest.json`, `pyproject.toml`, `__init__.py`, and prepends a new section to `CHANGELOG.md`.

```python
# scripts/update_version.py (Simplified Core Logic)
import json
import re
import sys
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_MANIFEST_PATH = _BASE_DIR / "manifest.json"

def bump_version(spec: str):
    data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    current = data["version"]

    major, minor, patch = map(int, current.split("."))
    if spec == "major":
        major += 1; minor = 0; patch = 0
    elif spec == "minor":
        minor += 1; patch = 0
    elif spec == "patch":
        patch += 1
    elif re.match(r"^\d+\.\d+\.\d+$", spec):
        major, minor, patch = map(int, spec.split("."))
    else:
        print("Usage: update_version.py <major|minor|patch|X.Y.Z>")
        sys.exit(1)

    new_version = f"{major}.{minor}.{patch}"
    data["version"] = new_version
    _MANIFEST_PATH.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # In a real scenario, this script also regex-replaces the version
    # inside pyproject.toml and src/yourname_peldrun/__init__.py

    print(f"Version updated: {current} -> {new_version}")
```

---

### 3.3 `changelog.py`
**Purpose:** Automatically generates release notes. It parses your Git commit history since the last tag, looks for **Conventional Commits** (`feat:`, `fix:`, `docs:`), and formats them into a Keep-a-Changelog compatible Markdown file.

```python
# scripts/changelog.py (Core Logic)
import subprocess
import re

def get_commits_since_last_tag():
    # Get the latest tag
    tag = subprocess.check_output(["git", "describe", "--tags", "--abbrev=0"], text=True).strip()
    # Get commits since that tag
    logs = subprocess.check_output(["git", "log", f"{tag}..HEAD", "--pretty=format:%s"], text=True).strip()
    return logs.split("\n") if logs else []

def generate_changelog(version):
    commits = get_commits_since_last_tag()
    categories = {"Added": [], "Fixed": [], "Changed": []}

    for msg in commits:
        if msg.startswith("feat:"):
            categories["Added"].append(f"- {msg.replace('feat:', '').strip()}")
        elif msg.startswith("fix:"):
            categories["Fixed"].append(f"- {msg.replace('fix:', '').strip()}")
        elif msg.startswith("refactor:") or msg.startswith("perf:"):
            categories["Changed"].append(f"- {msg.split(':', 1)[1].strip()}")

    # Render Markdown
    md = f"## [{version}] - 2025-01-01\n\n"
    for cat, items in categories.items():
        if items:
            md += f"### {cat}\n" + "\n".join(items) + "\n\n"

    print("Generated CHANGELOG section:")
    print(md)
```

---

### 3.4 `build_package.py`
**Purpose:** Creates the final `.zip` artifact.
**Crucial Detail:** It flattens the `src/` directory and sets deterministic timestamps (2020-01-01). This ensures that the resulting `.zip` file always has the **exact same SHA-256 hash** if the source code hasn't changed (Reproducible Builds).

```python
# scripts/build_package.py
import zipfile
import json
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent
_DIST_DIR = _BASE_DIR / "dist"

def build_zip():
    data = json.loads((_BASE_DIR / "manifest.json").read_text(encoding="utf-8"))
    version = data["version"]
    zip_path = _DIST_DIR / f"{data['id']}-{version}.zip"

    _DIST_DIR.mkdir(exist_ok=True)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Manifest and Requirements at root
        zf.write(_BASE_DIR / "manifest.json", "manifest.json")
        zf.write(_BASE_DIR / "requirements.txt", "requirements.txt")

        # 2. Flatten src/ package into root
        src_pkg = _BASE_DIR / "src" / f"{data['id'].replace('-', '_')}_peldrun"
        for file in src_pkg.rglob("*"):
            if file.is_file() and "__pycache__" not in file.parts:
                # Remove 'src/' from the path inside the zip
                arcname = file.relative_to(_BASE_DIR / "src")

                # Deterministic ZipInfo
                info = zipfile.ZipInfo(str(arcname), date_time=(2020, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16 # Standard file permissions

                zf.writestr(info, file.read_bytes())

    print(f"Built {zip_path}")

if __name__ == "__main__":
    build_zip()
```

---

### 3.5 `verify_package.py`
**Purpose:** Simulates PELDRUN's strict install-time security checks *before* you upload the package. It verifies that the ZIP has no path traversal (`../`), no symlinks, and calculates the final checksum.

```python
# scripts/verify_package.py (Core Logic)
import zipfile
import hashlib
from pathlib import Path

def verify_zip(zip_path: Path):
    print(f"Verifying {zip_path.name}...")

    # 1. Calculate SHA-256
    hasher = hashlib.sha256(zip_path.read_bytes())
    print(f"SHA-256: {hasher.hexdigest()}")

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()

        # 2. Check for Manifest at root
        if "manifest.json" not in names:
            print("[err] manifest.json missing from root!")
            return False

        # 3. Security Checks
        for name in names:
            if ".." in name or name.startswith("/"):
                print(f"[err] Zip Slip vulnerability detected: {name}")
                return False

            info = zf.getinfo(name)
            # Check for symlinks (Unix mode)
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                print(f"[err] Symlink detected: {name}")
                return False

    print("[ok] ZIP verification passed. Safe for PELDRUN.")
    return True
```

---

### 3.6 `generate_catalog_entry.py`
**Purpose:** Generates `catalog-entry.json`. This file is **NOT** part of the ZIP. It is the metadata file used by the PELDRUN Store to display the provider, its brand color, and its download URL.

```python
# scripts/generate_catalog_entry.py
import json
import argparse
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent.parent

def generate_entry(download_url, brand_color, icon):
    manifest = json.loads((_BASE_DIR / "manifest.json").read_text(encoding="utf-8"))

    entry = {
        "id": manifest["id"],
        "name": manifest["name"],
        "version": manifest["version"],
        "transport": manifest["transport"],
        "features": manifest.get("features", {}),
        "permissions": manifest.get("permissions", {}),

        # Presentation data (CLI args)
        "brand_color": brand_color,
        "icon": icon,
        "icon_type": "name",

        # Distribution data
        "download_url": download_url,
        # (Assumes you ran verify_package.py and saved the hash)
        "checksum_sha256": "PLACEHOLDER_SHA256_FROM_VERIFY_SCRIPT",
    }

    # Automatically derive Developer block from Manifest Author
    if manifest.get("author"):
        entry["developer"] = {
            "name": manifest["author"],
            "url": manifest.get("author_url", ""),
            "verified": False
        }

    out_path = _BASE_DIR / "dist" / "catalog-entry.json"
    out_path.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    print(f"Generated {out_path}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--download-url", required=True)
    parser.add_argument("--brand-color", default="#000000")
    parser.add_argument("--icon", default="default")
    args = parser.parse_args()

    generate_entry(args.download_url, args.brand_color, args.icon)
```

---

## 4. CI/CD Workflows

### `validate.yml`
Runs on every pull request. It ensures the developer hasn't broken the manifest, and that the code builds successfully.
```yaml
name: Validate
on: [pull_request]
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt pytest
      - run: python scripts/validate_manifest.py
      - run: python scripts/build_package.py
      - run: python scripts/verify_package.py
```

### `release.yml`
Runs only when you push a tag (e.g., `v1.0.0`). It builds the ZIP, generates the catalog entry, and creates a GitHub Release automatically.
```yaml
name: Release
on:
  push:
    tags: ['v*']
permissions:
  contents: write
jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - run: pip install -r requirements.txt
      - run: python scripts/build_package.py
      - run: python scripts/verify_package.py
      # Generates the catalog-entry.json using the GitHub Release URL
      - run: python scripts/generate_catalog_entry.py --download-url "https://github.com/${{ github.repository }}/releases/download/${{ github.ref_name }}/yourname-1.0.0.zip" --brand-color "#F38020" --icon yourname
      - uses: softprops/action-gh-release@v2
        with:
          files: |
            dist/*.zip
            dist/catalog-entry.json
```

---

## 5. Step-by-Step Development Lifecycle

If you are a new developer, here is the exact sequence of commands you will run from your terminal.

**1. Start a feature:**
```bash
git checkout -b feat/add-vision-support
# Write code in src/yourname_peldrun/adapter.py
```

**2. Test locally:**
```bash
python scripts/validate_manifest.py
pytest tests/
```

**3. Bump the version & generate changelog:**
```bash
python scripts/update_version.py minor
# This updates manifest.json, pyproject.toml, and CHANGELOG.md
```

**4. Build and Verify:**
```bash
python scripts/build_package.py
python scripts/verify_package.py
# Note the SHA-256 hash printed at the end.
```

**5. Commit and Tag:**
```bash
git add -A
git commit -m "feat: add vision support"
git push origin feat/add-vision-support
# (Create a PR, get it merged to main)
```

**6. Release:**
```bash
git checkout main
git pull origin main
git tag -a v1.1.0 -m "Release v1.1.0"
git push origin v1.1.0
```
*(The GitHub Action takes over from here, building the artifact and creating the Release).*

**7. Publish to Catalog:**
Navigate to `peldrun-provider-catalog` on GitHub -> Actions -> Run `import_catalog_entry.py` with the URL of your new `catalog-entry.json`.
