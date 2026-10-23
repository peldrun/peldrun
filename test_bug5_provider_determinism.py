# test_bug5_provider_determinism.py
"""
Verifies that `SourceControlFacade._default_provider_id` is
deterministic when more than one non-GitHub provider is connected.

Pre-fix behavior: iterated `self._connections` (a dict) and returned
the first matching ProviderId. Dict order follows insertion order,
which depends on the order of records in `connections.json`. With
two installs or two restarts, the same call could return DIFFERENT
providers.

Post-fix behavior: sorts candidates by `ProviderId.value` and
returns the smallest. The result is identical across processes and
restarts.

Run from the project root:
    python test_bug5_provider_determinism.py
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path

_BACKEND_ROOT = _Path(__file__).resolve().parent / "backend"
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from backend.omweb.services.source_control_facade import SourceControlFacade


def _facade_with(keys):
    """
    Build a SourceControlFacade WITHOUT running __init__ (which does a
    lot of setup). We only need `_default_provider_id`, which reads
    `self._connections`, so a plain instance with that attribute set
    is enough.
    """
    facade = object.__new__(SourceControlFacade)
    facade._connections = {k: None for k in keys}
    return facade


def test_github_wins() -> None:
    facade = _facade_with(["gitlab", "github", "bitbucket"])
    assert facade._default_provider_id().value == "github"
    print("[PASS] github_wins (github > everything)")


def test_single_non_github() -> None:
    facade = _facade_with(["gitlab"])
    assert facade._default_provider_id().value == "gitlab"
    print("[PASS] single_non_github")


def test_multi_is_deterministic() -> None:
    # `bitbucket` < `gitlab` alphabetically, so the fix must pick
    # bitbucket regardless of insertion order.
    for order in (["gitlab", "bitbucket"], ["bitbucket", "gitlab"]):
        facade = _facade_with(order)
        results = {facade._default_provider_id().value for _ in range(20)}
        assert results == {"bitbucket"}, (
            f"order={order} produced non-deterministic set {results}"
        )
    print(
        "[PASS] multi_is_deterministic "
        "(both insertion orders → 'bitbucket')"
    )


def test_no_connections_returns_github() -> None:
    facade = _facade_with([])
    assert facade._default_provider_id().value == "github"
    print("[PASS] no_connections_returns_github")


def test_unknown_provider_key_skipped() -> None:
    facade = _facade_with(["not_a_real_provider", "gitlab"])
    assert facade._default_provider_id().value == "gitlab"
    print("[PASS] unknown_provider_key_skipped")


def test_log_spam_suppressed() -> None:
    """
    The "multiple providers" warning must be emitted at most once
    per distinct provider-set per instance, not on every call.

    This is the regression test for the log-spam fix: before the
    fix, 50 calls to `_default_provider_id` produced 50 identical
    log lines; after the fix, they produce exactly 1.
    """
    import logging as _logging

    class _Counter(_logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.count = 0

        def emit(self, record: _logging.LogRecord) -> None:
            if "Multiple non-GitHub providers are connected" in record.getMessage():
                self.count += 1

    counter = _Counter()
    target_logger = _logging.getLogger(
        "backend.omweb.services.source_control_facade"
    )
    target_logger.addHandler(counter)
    target_logger.setLevel(_logging.WARNING)
    try:
        facade = _facade_with(["gitlab", "bitbucket"])
        for _ in range(50):
            facade._default_provider_id()
        assert counter.count == 1, (
            f"warning emitted {counter.count} times for 50 calls "
            f"on the same instance; expected exactly 1"
        )

        # A SECOND instance starts with its own tracking set,
        # so it MUST emit its own single warning. This is
        # correct behavior: two instances may represent
        # different connection configurations.
        facade2 = _facade_with(["gitlab", "bitbucket"])
        for _ in range(50):
            facade2._default_provider_id()
        assert counter.count == 2, (
            f"warning emitted {counter.count} times across 2 "
            f"instances; expected exactly 2 (one per instance)"
        )

        # Within a single instance, a NEW provider-set must
        # emit exactly one additional warning.
        facade3 = _facade_with(["gitlab", "bitbucket"])
        for _ in range(10):
            facade3._default_provider_id()
        # Mutate the connection set to a different combination.
        facade3._connections = {
            "gitlab": None,
            "bitbucket": None,
            "github": None,  # github wins, so no "multiple" warning
        }
        for _ in range(10):
            facade3._default_provider_id()
        # No new "multiple" warning because github is now present.
        assert counter.count == 3, (
            f"warning emitted {counter.count} times; expected 3 "
            f"(1 per instance with non-GitHub-only set)"
        )
    finally:
        target_logger.removeHandler(counter)

    print("[PASS] log_spam_suppressed (1 warning per provider-set per instance)")


def main():
    print("=" * 60)
    print("Bug 5 (deterministic provider selection) Tests")
    print("=" * 60)
    test_github_wins()
    test_single_non_github()
    test_multi_is_deterministic()
    test_no_connections_returns_github()
    test_unknown_provider_key_skipped()
    test_log_spam_suppressed()
    print("=" * 60)
    print("ALL TESTS PASSED")
    print("=" * 60)


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"\n[FAIL] {exc}", file=sys.stderr)
        sys.exit(1)
    except Exception as exc:
        import traceback
        print(f"\n[ERROR] {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        sys.exit(2)
