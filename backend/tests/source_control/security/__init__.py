# backend/tests/source_control/security/__init__.py
"""
Security-focused regression tests for the Source Control subsystem.

This package contains tests that ATTEMPT bypasses and confirm the
subsystem rejects them. Every module here has the same shape:

    - Each test states the ATTACK explicitly in its docstring.
    - Each test asserts on observable behavior, not on internal
      state.
    - Each test is a regression test: if the subsystem ever allows
      the bypass, CI fails loudly.

Modules in this package:
    - `test_approval_bypass`     : attempts to consume, swap, forge,
                                   or replay approval tickets.
    - `test_command_injection`   : attempts to smuggle shell
                                   metacharacters through arguments
                                   that reach the Git subprocess.
    - `test_path_traversal`      : attempts to escape the workspace
                                   root through `..`, symlinks, or
                                   absolute paths.
"""
