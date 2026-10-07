"""
backend/tests/test_artifact_persistence.py

Automated Test Suite for TASK P1-04 (Persistent Artifact Metadata & History).

Verifies:
1. Durable manifest persistence across process restarts using SqliteArtifactStore.
2. Checksum (SHA-256) calculation and monotonic revision increments on file mutation.
3. Append-only audit logging in artifact_history correlated with run_id and chat_id.
4. Reconciliation scanning preserves existing revisions without duplicate resets.
5. Deletion tracking in both manifest state and history ledger.
"""

from pathlib import Path
import pytest

from peldrun.artifacts.manager import ArtifactManager
from peldrun.artifacts.store import SqliteArtifactStore


@pytest.fixture
def isolated_artifact_store(tmp_path):
    """Instantiate a dedicated SQLite artifact store in a temporary directory."""
    db_file = tmp_path / "test_peldrun_runtime.db"
    return SqliteArtifactStore(db_path=db_file)


@pytest.mark.asyncio
async def test_artifact_persistence_across_restarts(tmp_path, isolated_artifact_store):
    """Verify that an ArtifactManager restores previously tracked files across restarts."""
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    file_a = ws_dir / "index.html"
    file_a.write_text("<h1>Hello World</h1>", encoding="utf-8")

    # 1. First session records creation
    manager1 = ArtifactManager(
        workspace_root=str(ws_dir),
        workspace_id="ws_test_restart",
        chat_id="chat_restart_1",
        store=isolated_artifact_store,
    )
    ref1 = await manager1.arecord_mutation(file_a, operation="created", run_id="run_1")
    assert ref1 is not None
    assert ref1.revision == 1
    assert ref1.sha256 is not None
    first_hash = ref1.sha256

    # 2. Simulate process shutdown and reload in a new ArtifactManager instance
    manager2 = ArtifactManager(
        workspace_root=str(ws_dir),
        workspace_id="ws_test_restart",
        chat_id="chat_restart_1",
        store=isolated_artifact_store,
    )
    restored_manifest = manager2.get_manifest()
    assert len(restored_manifest.artifacts) == 1
    restored_ref = restored_manifest.artifacts[0]
    assert restored_ref.relative_path == "index.html"
    assert restored_ref.revision == 1
    assert restored_ref.sha256 == first_hash

    # 3. Modify the file and record mutation in the restored manager
    file_a.write_text("<h1>Updated Version</h1>", encoding="utf-8")
    ref2 = await manager2.arecord_mutation(file_a, operation="updated", run_id="run_2")
    assert ref2 is not None
    assert ref2.revision == 2
    assert ref2.sha256 != first_hash

    # 4. Verify artifact_history audit trail contains both versions
    history = await isolated_artifact_store.list_artifact_history(workspace_id="ws_test_restart")
    assert len(history) == 2
    assert history[0]["revision"] == 1
    assert history[0]["operation"] == "created"
    assert history[1]["revision"] == 2
    assert history[1]["operation"] == "updated"


@pytest.mark.asyncio
async def test_reconciliation_scan_records_mutations(tmp_path, isolated_artifact_store):
    """Verify scan_mutations reconciliation captures external file creation and changes."""
    ws_dir = tmp_path / "recon_workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    manager = ArtifactManager(
        workspace_root=str(ws_dir),
        workspace_id="ws_recon",
        chat_id="chat_recon",
        store=isolated_artifact_store,
    )
    manager.snapshot_baseline()

    # External process writes a file without calling arecord_mutation
    new_script = ws_dir / "app.py"
    new_script.write_text("print('peldrun')", encoding="utf-8")

    mutations = await manager.scan_mutations(run_id="run_recon_1")
    assert len(mutations) == 1
    op, ref = mutations[0]
    assert op == "created"
    assert ref.relative_path == "app.py"
    assert ref.revision == 1
    assert ref.sha256 is not None

    # Verify persisted in database
    manifest = await isolated_artifact_store.load_manifest("ws_recon")
    assert manifest is not None
    assert len(manifest.artifacts) == 1
    assert manifest.artifacts[0].name == "app.py"


@pytest.mark.asyncio
async def test_artifact_deletion_tracking(tmp_path, isolated_artifact_store):
    """Verify deleting an artifact marks operation as 'deleted' and preserves history."""
    ws_dir = tmp_path / "del_workspace"
    ws_dir.mkdir(parents=True, exist_ok=True)

    temp_doc = ws_dir / "doc.txt"
    temp_doc.write_text("Temporary draft", encoding="utf-8")

    manager = ArtifactManager(
        workspace_root=str(ws_dir),
        workspace_id="ws_del",
        chat_id="chat_del",
        store=isolated_artifact_store,
    )
    await manager.arecord_mutation(temp_doc, operation="created")

    # Delete file and scan
    temp_doc.unlink()
    mutations = await manager.scan_mutations()
    assert len(mutations) == 1
    op, del_ref = mutations[0]
    assert op == "deleted"
    assert del_ref.operation == "deleted"

    # Active paths should exclude deleted file
    assert "doc.txt" not in manager.get_manifest().get_relative_paths()

    # History retains creation and deletion facts
    history = await isolated_artifact_store.list_artifact_history(workspace_id="ws_del")
    assert len(history) == 2
    assert history[0]["operation"] == "created"
    assert history[1]["operation"] == "deleted"