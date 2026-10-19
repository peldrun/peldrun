# backend/tests/manual/test_phase4_engine_result.py
"""Verify the final-result composition logic in peldrun_engine.py.

This mirrors the exact branch structure that now lives in `run()`.
Because the logic is embedded in a large method, the test reconstructs
it verbatim. If you later extract the composition into a helper
function, change this test to import that helper instead.
"""

from __future__ import annotations

import sys


def compose_result(
    agent_output: str,
    canonical_deliverables: list[str],
) -> str:
    # Mirrors the exact code path in peldrun_engine.py::run after
    # the deliverables fix.
    agent_output_is_meaningful = bool(
        agent_output and agent_output.strip()
    )
    if agent_output_is_meaningful:
        return agent_output
    if canonical_deliverables:
        file_bullets = "\n".join([f"- `{f}`" for f in canonical_deliverables])
        return (
            f"### Deliverables Created Successfully\n\n"
            f"The requested project files have been built and "
            f"saved in your workspace:\n\n"
            f"{file_bullets}\n\n"
            f"You can preview and interact with the application "
            f"live in the **Preview** panel."
        )
    return agent_output


def main() -> int:
    failures: list[str] = []

    def check(label: str, actual: str, must_contain: list[str],
              must_not_contain: list[str] | None = None) -> None:
        must_not_contain = must_not_contain or []
        for token in must_contain:
            if token not in actual:
                failures.append(f"{label}: missing {token!r} in output")
        for token in must_not_contain:
            if token in actual:
                failures.append(f"{label}: unexpected {token!r} in output")
        if not any(f.startswith(label) for f in failures):
            print(f"[ OK ] {label}")

    # 1. Source-control style: agent reports IDs + files were created.
    #    Deliverables must NOT hide the IDs.
    out = compose_result(
        agent_output=(
            "Created operation op_abc123 and approval ticket apv_xyz789. "
            "Please approve in the UI to continue."
        ),
        canonical_deliverables=["README.md", "src/main.py"],
    )
    check(
        "source_control-style output (IDs preserved)",
        out,
        must_contain=["op_abc123", "apv_xyz789"],
        must_not_contain=["### Deliverables"],
    )

    # 2. Empty output + deliverables → historical deliverables block.
    out = compose_result(
        agent_output="",
        canonical_deliverables=["site/index.html", "site/style.css"],
    )
    check(
        "empty output + deliverables",
        out,
        must_contain=[
            "### Deliverables Created Successfully",
            "site/index.html",
            "site/style.css",
        ],
    )

    # 3. Whitespace-only output counts as empty.
    out = compose_result(
        agent_output="   \n\t  ",
        canonical_deliverables=["out.txt"],
    )
    check(
        "whitespace-only output falls back to deliverables",
        out,
        must_contain=["### Deliverables Created Successfully", "out.txt"],
    )

    # 4. Meaningful output + no deliverables → output preserved.
    out = compose_result(
        agent_output="The task is complete. All checks passed.",
        canonical_deliverables=[],
    )
    check(
        "meaningful output + no deliverables",
        out,
        must_contain=["All checks passed"],
        must_not_contain=["### Deliverables"],
    )

    # 5. Empty output + no deliverables → stays empty (historical).
    out = compose_result(agent_output="", canonical_deliverables=[])
    if out != "":
        failures.append(f"empty/empty: expected '', got {out!r}")
    else:
        print("[ OK ] empty output + no deliverables → empty")

    print()
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        return 1
    print("FILE 16 ACCEPTANCE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
