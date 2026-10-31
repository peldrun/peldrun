# backend/test_skills_pipeline.py
"""
End-to-end smoke test for the PELDRUN Skills subsystem.

Run from backend/:
    python test_skills_pipeline.py

This script exercises the full pipeline without needing a live server:
  1. Builds a sample code_review skill package as a .zip in memory.
  2. Installs it via POST /api/skills/install.
  3. Lists skills via GET /api/skills.
  4. Previews instructions via GET /api/skills/{id}/preview.
  5. Assigns the skill to the 'peldrun' agent via PUT /api/store/agents/{id}/skills.
  6. Verifies engine-time injection via resolve_skill_injection().
  7. Cleans up: resets assignment and deletes the skill.

Exits with code 0 on success, 1 on any failure.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys
import zipfile

from fastapi.testclient import TestClient

from omweb.main import app


SKILL_ID = "code_review"
SKILL_VERSION = "1.0.0"
AGENT_ID = "peldrun"


def _build_skill_zip() -> bytes:
    """Build the test skill package in memory and return the .zip bytes."""
    skill_md = """---
name: code_review
description: Review code for bugs, security issues, and maintainability problems.
---

# Code Review

## Instructions

1. Inspect the relevant source files.
2. Identify correctness, security, and maintainability issues.
3. Prioritize findings by severity (Critical, High, Medium, Low).
4. Cite file paths and line numbers when available.
5. Distinguish verified defects from speculative recommendations.

## Output Format

For each issue, report:
- Severity: Critical / High / Medium / Low
- Location: file path + line range
- Issue: one-sentence description
- Evidence: code excerpt or reasoning
- Fix: recommended change

If no issues are found, say so explicitly.
"""

    manifest = {
        "schema_version": "1.0.0",
        "id": SKILL_ID,
        "version": SKILL_VERSION,
        "name": "Code Review",
        "description": (
            "Review code for bugs, security issues, and maintainability."
        ),
        "short_description": "Systematic code review checklist.",
        "tags": ["review", "audit", "security"],
        "author": {"name": "PELDRUN Team"},
        "trust": "verified",
        "capabilities": {
            "chat_invocable": True,
            "agent_assignable": True,
            "workflow_node": False,
        },
        "execution": {
            "type": "instructions",
            "instructions_file": "SKILL.md",
        },
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        zf.writestr("SKILL.md", skill_md)
    return buf.getvalue()


def _check(label: str, condition: bool, detail: str = "") -> None:
    """Print PASS/FAIL and raise if the condition fails."""
    marker = "PASS" if condition else "FAIL"
    print(f"[{marker}] {label}" + (f" — {detail}" if detail else ""))
    if not condition:
        raise AssertionError(f"{label} failed. {detail}")


def main() -> int:
    client = TestClient(app)
    zip_bytes = _build_skill_zip()
    print(f"zip size: {len(zip_bytes)} bytes")

    # ── Step 1: Install ─────────────────────────────────────────────
    print("\n=== Step 1: Install ===")
    r = client.post(
        "/api/skills/install",
        files={
            "file": (
                f"{SKILL_ID}-{SKILL_VERSION}.zip",
                zip_bytes,
                "application/zip",
            )
        },
    )
    body = r.json()
    _check(
        "install HTTP 200",
        r.status_code == 200,
        f"status={r.status_code}",
    )
    _check("install status=success", body.get("status") == "success")
    skill = body.get("skill") or {}
    _check(
        "installed skill id",
        skill.get("id") == SKILL_ID,
        f"id={skill.get('id')}",
    )
    _check(
        "installed skill version",
        skill.get("version") == SKILL_VERSION,
        f"version={skill.get('version')}",
    )

    # ── Step 2: List ────────────────────────────────────────────────
    print("\n=== Step 2: List ===")
    r = client.get("/api/skills")
    body = r.json()
    _check("list HTTP 200", r.status_code == 200)
    total = body.get("total", 0)
    _check("total >= 1", total >= 1, f"total={total}")
    ids = [s["id"] for s in body.get("skills", [])]
    _check(
        f"{SKILL_ID} appears in list",
        SKILL_ID in ids,
        f"ids={ids}",
    )

    # ── Step 3: Search ──────────────────────────────────────────────
    print("\n=== Step 3: Search ===")
    r = client.get("/api/skills?search=code")
    body = r.json()
    _check("search HTTP 200", r.status_code == 200)
    result_ids = [s["id"] for s in body.get("skills", [])]
    _check(
        f"{SKILL_ID} found by search 'code'",
        SKILL_ID in result_ids,
        f"ids={result_ids}",
    )

    # ── Step 4: Preview ─────────────────────────────────────────────
    print("\n=== Step 4: Preview ===")
    r = client.get(f"/api/skills/{SKILL_ID}/preview")
    body = r.json()
    _check("preview HTTP 200", r.status_code == 200)
    instructions = body.get("instructions", "")
    _check(
        "instructions contain marker",
        "Severity: Critical" in instructions,
        f"length={len(instructions)}",
    )

    # ── Step 5: Assign to agent ─────────────────────────────────────
    print("\n=== Step 5: Assign to agent ===")
    r = client.put(
        f"/api/store/agents/{AGENT_ID}/skills",
        json={"disabled_skills": [], "extra_skills": [SKILL_ID]},
    )
    body = r.json()
    _check("assign HTTP 200", r.status_code == 200, f"body={body}")
    effective = body.get("effective_skills", [])
    _check(
        f"effective_skills contains {SKILL_ID}",
        SKILL_ID in effective,
        f"effective={effective}",
    )

    # ── Step 6: Injection (assigned only) ───────────────────────────
    print("\n=== Step 6: Injection (assigned only) ===")
    from omweb.skills.assignments import resolve_skill_injection

    inj = resolve_skill_injection(AGENT_ID, [])
    injected_ids = [s["id"] for s in inj.skills]
    _check(
        "assigned injection loads skill",
        SKILL_ID in injected_ids,
        f"injected={injected_ids}",
    )
    _check(
        "injection text has BEGIN marker",
        f"--- BEGIN SKILL: {SKILL_ID}@{SKILL_VERSION} ---" in inj.text,
    )
    _check(
        "injection source is 'assigned'",
        any(
            s["id"] == SKILL_ID and s["source"] == "assigned"
            for s in inj.skills
        ),
    )

    # ── Step 7: Injection (invoked via /) ───────────────────────────
    print("\n=== Step 7: Injection (invoked via /) ===")
    inj2 = resolve_skill_injection(AGENT_ID, [SKILL_ID])
    injected_ids2 = [s["id"] for s in inj2.skills]
    _check(
        "invoked injection loads skill",
        SKILL_ID in injected_ids2,
        f"injected={injected_ids2}",
    )
    _check(
        "invoked source overrides assigned",
        any(
            s["id"] == SKILL_ID and s["source"] == "invoked"
            for s in inj2.skills
        ),
    )

    # ── Step 8: Toggle (disable) ────────────────────────────────────
    print("\n=== Step 8: Toggle disable ===")
    r = client.post(f"/api/skills/{SKILL_ID}/toggle")
    body = r.json()
    _check("toggle HTTP 200", r.status_code == 200)
    _check(
        "skill is now disabled",
        body.get("skill", {}).get("status") == "disabled",
    )

    inj3 = resolve_skill_injection(AGENT_ID, [SKILL_ID])
    _check(
        "disabled skill is skipped",
        len(inj3.skills) == 0,
        f"skills={inj3.skills}",
    )
    _check(
        "disabled skill produces warning",
        any("disabled" in w.lower() for w in inj3.warnings),
        f"warnings={inj3.warnings}",
    )

    # Re-enable
    r = client.post(f"/api/skills/{SKILL_ID}/toggle")
    _check(
        "re-enable HTTP 200",
        r.status_code == 200 and r.json()["skill"]["status"] == "active",
    )

    # ── Step 9: Reset assignment ────────────────────────────────────
    print("\n=== Step 9: Reset assignment ===")
    r = client.put(
        f"/api/store/agents/{AGENT_ID}/skills",
        json={"disabled_skills": [], "extra_skills": []},
    )
    _check(
        "reset HTTP 200",
        r.status_code == 200,
        f"body={r.json()}",
    )
    _check(
        "extra_skills cleared",
        r.json().get("extra_skills") == [],
    )

    # ── Step 10: Delete ─────────────────────────────────────────────
    print("\n=== Step 10: Delete ===")
    r = client.delete(f"/api/skills/{SKILL_ID}")
    _check(
        "delete HTTP 200",
        r.status_code == 200,
        f"body={r.json() if r.status_code != 200 else ''}",
    )
    _check("deleted=true", r.json().get("deleted") is True)

    r = client.get("/api/skills")
    ids_after = [s["id"] for s in r.json().get("skills", [])]
    _check(
        f"{SKILL_ID} no longer listed",
        SKILL_ID not in ids_after,
        f"ids={ids_after}",
    )

    print("\n===========================================")
    print("ALL CHECKS PASSED")
    print("===========================================")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nFATAL: {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        import traceback

        traceback.print_exc()
        sys.exit(1)
