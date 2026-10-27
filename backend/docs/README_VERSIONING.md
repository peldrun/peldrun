

```markdown
# PELDRUN Version Management

PELDRUN tracks **four independent version streams**:

| Stream     | Source of truth                          | Scope                          |
|------------|------------------------------------------|--------------------------------|
| `app`      | `VERSION.json` (repo root)               | Whole application              |
| `omweb`    | `backend/omweb/VERSION.json`             | Web API & routing layer        |
| `peldrun`  | `backend/peldrun/VERSION.json`           | Core reasoning engine          |
| `frontend` | `frontend/VERSION.json`                  | Next.js web UI                 |

All versions follow [SemVer 2.0.0](https://semver.org/): `MAJOR.MINOR.PATCH`.
The `v` prefix is added **only** in git tags (`v1.0.0`, `omweb-v0.20.1`),
never in the files themselves.

---

## Daily workflow

### 1. See current versions

```powershell
python scripts/version_manager.py show
```

### 2. See what changed since last release

```powershell
python scripts/version_manager.py detect
```

Output example:

```
[omweb     ] CHANGED  3 file(s)
    - backend/omweb/config.py
    - backend/omweb/resource_monitor/config.py
    - backend/omweb/routers/config_rtr.py
[peldrun   ] clean
[frontend  ] clean

Recommendation: run `bump <component> patch` or `release`.
```

### 3. Bump a single component

```powershell
# Patch bump (bug fix)
python scripts/version_manager.py bump omweb patch
# → omweb: 0.20.1 → 0.20.2
# → app:   1.0.0  → 1.0.1  (auto)

# Minor bump (new feature, backwards-compatible)
python scripts/version_manager.py bump omweb minor
# → omweb: 0.20.1 → 0.21.0
# → app:   1.0.0  → 1.0.1  (auto patch)

# Major bump (breaking change)
python scripts/version_manager.py bump omweb major
# → omweb: 0.20.1 → 1.0.0
# → app:   1.0.0  → 1.0.1  (auto patch)
```

**Default behavior:** every component bump auto-bumps `app` by one **patch** step.
Disable with `--no-app-bump` if you want full manual control.

### 4. Release everything at once

```powershell
# Auto-detect changed components, bump each by patch, bump app
python scripts/version_manager.py release

# Release with app-minor (for a feature batch)
python scripts/version_manager.py release --app-minor

# Release with app-major (for a milestone)
python scripts/version_manager.py release --app-major
```

Output:

```
✅ omweb: 0.20.1 → 0.20.2
✅ peldrun: 0.1.0 → 0.1.1
✅ app: 1.0.0 → 1.1.0 (minor)

Components released: omweb, peldrun
```

### 5. Verify consistency

```powershell
python scripts/version_manager.py verify
# ✅ All versions are in sync.
```

If out of sync:

```powershell
python scripts/version_manager.py sync-app
# Resyncs app.components[*].version from each component's VERSION.json
```

---

## Where versions appear

### 1. Source files

- `VERSION.json` (repo root)
- `backend/omweb/VERSION.json`
- `backend/peldrun/VERSION.json`
- `frontend/VERSION.json`

### 2. HTTP API

`GET /api/system/version` returns:

```json
{
  "app": { "version": "1.0.0", "released_at": "...", "released_commit": "..." },
  "components": {
    "omweb":    { "version": "0.20.1", "released_commit": "..." },
    "peldrun":  { "version": "0.1.0",  "released_commit": "..." },
    "frontend": { "version": "0.1.0",  "released_commit": "..." }
  },
  "all_in_sync": true
}
```

### 3. UI

Settings → System tab displays:
- `VersionCard` — app version, component versions, sync status
- `BackendStatusCard` — backend hostname, PID, uptime

---

## Git integration (recommended)

### Optional pre-push hook

Create `.git/hooks/pre-push`:

```bash
#!/bin/sh
python scripts/version_manager.py verify || {
    echo ""
    echo "❌ Version files out of sync."
    echo "   Fix with: python scripts/version_manager.py sync-app"
    exit 1
}
```

Make it executable:

```bash
chmod +x .git/hooks/pre-push
```

### Version tags

After releasing:

```powershell
# Tag the whole app
git tag v1.0.0

# Tag individual components (optional)
git tag omweb-v0.20.1
git tag peldrun-v0.1.0
git tag frontend-v0.1.0

# Push all tags
git push --tags
```

---

## FAQ

### Q: Do I have to bump every component?

No. `release` only bumps components that have changed files since
their last `released_commit`. Untouched components stay at their
current version.

### Q: Can I bump app without touching components?

```powershell
# Manually edit VERSION.json's "version" field and update "history"
# OR use a future `app-bump` subcommand (not yet implemented)
```

### Q: What happens if I forget to bump?

- Code still runs fine.
- `detect` will keep showing "CHANGED" for that component.
- The API/UI `all_in_sync` flag will remain `true` (it only checks
  file-to-file consistency, not "did you forget to bump").
- The `pre-push` hook (if installed) will refuse the push only if
  files are out of sync — not if they're simply stale.

### Q: Where do I record change summaries?

Each `VERSION.json` has a `history` array:

```json
"history": [
  {
    "version": "0.20.1",
    "at": "2026-10-27T15:30:00Z",
    "kind": "patch",
    "summary": "Fix storage path unification for CWD independence"
  }
]
```

Pass a summary during bump:

```powershell
python scripts/version_manager.py bump omweb patch --summary "unify storage paths"
```

---

## Reference: SemVer quick guide

| Change type                              | Bump    | Example           |
|------------------------------------------|---------|-------------------|
| Bug fix, no API change                   | `patch` | `0.20.1 → 0.20.2` |
| New feature, backwards-compatible        | `minor` | `0.20.1 → 0.21.0` |
| Breaking change                          | `major` | `0.20.1 → 1.0.0`  |
| Pre-1.0 projects: any change is breaking | `minor` | `0.1.0 → 0.2.0`   |
```

---
