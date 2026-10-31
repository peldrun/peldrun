```markdown
# Skill Examples

Each subdirectory is a complete, ready-to-install skill package.

## Packaging a skill

A skill package is a `.zip` whose root contains `manifest.json` and the
declared instructions file (`SKILL.md` by default):

```
code-review.zip
├── manifest.json
└── SKILL.md
```

The `code-review/` directory is included as source; to install it, zip
the **contents** (not the directory itself):

### PowerShell

```powershell
Compress-Archive -Path ".\code-review\*" -DestinationPath ".\code-review.zip" -Force
```

### Bash

```bash
cd code-review && zip -r ../code-review.zip . && cd ..
```

## Installing

### Via the API

```bash
curl -X POST http://127.0.0.1:8088/api/skills/install \
  -F "file=@code-review.zip"
```

### Via the Store (after P7 ships)

Navigate to `/store/skills` and click **Install Skill**.

## What happens on install

1. ZIP is validated (path safety, size caps, symlinks).
2. Contents are extracted to a staging directory.
3. `manifest.json` is validated against the JSON Schema.
4. `SKILL.md` frontmatter is parsed.
5. `icon_svg` (if present) is sanitized.
6. Version compatibility is checked.
7. The package is atomically installed at `storage/store/skills/<id>/`.
8. The registry is refreshed and the skill becomes visible immediately.

## Using a skill

- **From chat**: type `/code_review` in the composer (after P7 ships).
- **From an agent**: assign it in `/store/agents` → Code Architect → Skills.
- **From both**: assigned + invoked skills are merged at run time, with
  invoked taking priority on conflict.
```

---
