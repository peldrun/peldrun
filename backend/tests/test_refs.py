"""
Standalone test for the [FIX-5.4] context_refs persistence in session.json.
Must be run from the backend directory OR with backend on sys.path.
"""

import json
import shutil
import sys
import time
from pathlib import Path

# Ensure the backend root is on sys.path so `omweb` is importable
# even when this file is invoked from a subdirectory like `tests/`.
BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from omweb.project_manager import project_manager  # noqa: E402


def main() -> None:
    chat_id = "chat_test_refs_" + str(int(time.time()))

    # 1. Create a test session
    project_manager.save_chat_session(
        chat_id=chat_id,
        project_id="default_project",
        title="test",
        job_id="job_test_x",
        prompt="hi",
        events=[],
        result="",
        status="running",
    )

    # 2. Record a turn WITHOUT context_refs (legacy path)
    project_manager.record_turn_usage(
        chat_id=chat_id,
        turn_id="turn_1",
        turn_usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    )

    # 3. Record a turn WITH context_refs (new path)
    project_manager.record_turn_usage(
        chat_id=chat_id,
        turn_id="turn_2",
        turn_usage={"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        context_refs=[
            {"relPath": "report.md", "fileName": "report.md", "sizeBytes": 100}
        ],
    )

    # 4. Inspect session.json
    session_file = project_manager.storage_dir / "chats" / chat_id / "session.json"
    print("Session file:", session_file)
    print("Exists:", session_file.exists())

    data = json.loads(session_file.read_text(encoding="utf-8"))
    for turn in data.get("turns", []):
        has_refs = "context_refs" in turn
        refs = turn.get("context_refs")
        print(f"{turn.get('turn_id')} -> has_refs={has_refs} refs={refs}")

    # 5. Cleanup
    shutil.rmtree(project_manager.storage_dir / "chats" / chat_id, ignore_errors=True)
    print(f"\nCleaned up test chat: {chat_id}")


if __name__ == "__main__":
    main()
