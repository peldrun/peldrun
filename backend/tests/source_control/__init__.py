# D:\AI\peldrun\backend\tests\source_control\__init__.py
"""Source Control Integration Test Suite.

This package contains the automated unit, contract, security, and integration
tests verifying the Source Control subsystem of peldrun-core.

Coverage Domains:
1. Domain Models & Policy Invariants (models, enums, policies)
2. Credential Vault & Ephemeral Lifecycle (vault, token service, OAuth)
3. Secret Redaction & Leak Prevention (redaction engine)
4. GitHub Provider Client & Data Mapping (client, repositories, pull requests, checks)
5. Isolated Local Git Engine & Path Policies (workspace, diff, push, path traversal)
6. Cryptographic Approval Gating & Diff Hashing (approval gate, SHA-256 fingerprinting)
7. Durable Operation State Machine & Recovery (journal, state transitions, idempotency)
8. Agent Tools Contract Compliance (ToolResult encapsulation, zero credential leakage)
9. FastAPI HTTP Routing & Error Normalization (router endpoints, HTTP status mappings)
"""

from __future__ import annotations

# Suite identification metadata
SUITE_NAME = "peldrun_source_control_test_suite"
SUITE_VERSION = "1.0.0"

# Standard test fixtures constants
DUMMY_PROVIDER = "github"
DUMMY_REPO_ID = "12345678"
DUMMY_REPO_SLUG = "peldrun/sample-repo"
DUMMY_DEFAULT_BRANCH = "main"
DUMMY_FEATURE_BRANCH = "feature/test-task"
DUMMY_BASE_SHA = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
DUMMY_HEAD_SHA = "f9e8d7c6b5a432109876543210fedcba98765432"
DUMMY_DIFF_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

__all__ = [
    "SUITE_NAME",
    "SUITE_VERSION",
    "DUMMY_PROVIDER",
    "DUMMY_REPO_ID",
    "DUMMY_REPO_SLUG",
    "DUMMY_DEFAULT_BRANCH",
    "DUMMY_FEATURE_BRANCH",
    "DUMMY_BASE_SHA",
    "DUMMY_HEAD_SHA",
    "DUMMY_DIFF_SHA256",
]
