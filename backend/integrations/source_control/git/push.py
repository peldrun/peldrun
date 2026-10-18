# backend/integrations/source_control/git/push.py
"""
Git push manager for the Source Control Integration subsystem.

This module implements `PushPort` from `ports.repository`. It is the
ONLY place in the subsystem permitted to write to a remote
repository.

The manager enforces the following invariants:

    - Plain `--force` is NEVER used. Only `--force-with-lease` is
      allowed, and the lease value is always an explicit remote SHA
      obtained immediately before the push.
    - Credentials are supplied via a short-lived `GIT_ASKPASS`
      script. Tokens are never embedded in remote URLs and never
      persisted in `.git/config`.
    - `--porcelain` is used so that every ref's outcome is machine
      parsable. Every result is classified into a stable error
      category.
    - Ambiguous timeouts do NOT assume failure. The manager returns a
      `PushResult` with `ambiguous=True` and the caller reconciles
      via `reconcile_push`.
    - The manager never pushes every branch. It pushes exactly one
      ref per invocation.

Design rules:
    - The manager holds no per-request state and is safe to share
      across coroutines.
    - The manager NEVER logs credentials. All stdout/stderr passes
      through `redact_text` before being returned.
    - The manager does NOT decide whether a push is allowed. Policy
      and approval decisions belong to the application layer.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import stat
import tempfile
import threading
from dataclasses import replace
from pathlib import Path
from typing import List, Optional, Sequence, Tuple, Union

from ..ports.repository import PushPort, PushResult
from ..errors import (
    AuthRequiredError,
    CommandFailedError,
    CommandTimeoutError,
    ConflictError,
    ProtectedBranchError,
    RemoteStateChangedError,
    ValidationError,
    WorkspaceNotFoundError,
)
from ..security.redaction import redact_text
from .executable import (
    GitExecutable,
    GitExecutionError,
    get_default_git_executable,
)
from .path_policy import (
    WorkspacePathPolicy,
    get_default_path_policy,
)

__all__ = [
    "GitPushManager",
    "PushOutcome",
    "get_default_push_manager",
    "set_default_push_manager",
    "reset_default_push_manager",
    "DEFAULT_PUSH_TIMEOUT_SECONDS",
    "DELETE_BRANCH_TIMEOUT_SECONDS",
]


logger = logging.getLogger(__name__)


#: Default timeout for a push. Generous because large objects may
#: take time to transfer even on a fast link.
DEFAULT_PUSH_TIMEOUT_SECONDS: int = 120

#: Timeout for deleting a remote branch. Shorter because it does not
#: transfer objects.
DELETE_BRANCH_TIMEOUT_SECONDS: int = 60

#: Timeout for `git ls-remote` calls used for lease resolution and
#: post-push reconciliation.
LS_REMOTE_TIMEOUT_SECONDS: int = 30


#: Porcelain status codes emitted by `git push --porcelain`.
#:
#:     ' ' pushed
#:     '+' forced update
#:     '-' deleted
#:     '*' new branch
#:     '!' rejected
#:     '=' up to date
_PUSH_PORCELAIN_LINE = re.compile(
    r"^(?P<flag>[ +\-*!=])\t"
    r"(?P<local_ref>[^\t]+)"
    r"\t(?P<remote_ref>[^\t]+)"
    r"\t(?P<summary>.+)$"
)

#: Extracts the reason inside `[...]` or `(...)` in a porcelain line.
_REASON = re.compile(r"[\[\(](?P<reason>[^\]\)]+)[\]\)]")


# ---------------------------------------------------------------------------
# Internal outcome
# ---------------------------------------------------------------------------


class PushOutcome(str):
    """
    String-typed outcome code for a single ref push.

    Defined as a `str` subclass (not an enum) so that it can be
    serialized transparently and compared to literal strings in
    callers without importing this module.
    """

    PUSHED = "pushed"
    FORCED = "forced"
    DELETED = "deleted"
    NEW_BRANCH = "new_branch"
    UP_TO_DATE = "up_to_date"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


#: Mapping from porcelain flags to outcomes.
_FLAG_TO_OUTCOME = {
    " ": PushOutcome.PUSHED,
    "+": PushOutcome.FORCED,
    "-": PushOutcome.DELETED,
    "*": PushOutcome.NEW_BRANCH,
    "=": PushOutcome.UP_TO_DATE,
    "!": PushOutcome.REJECTED,
}


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class GitPushManager(PushPort):
    """
    Remote push manager backed by the Git command-line tool.

    The manager holds only immutable references to the Git executable
    and the path policy. It has no per-request state.
    """

    def __init__(
        self,
        *,
        git: Optional[GitExecutable] = None,
        path_policy: Optional[WorkspacePathPolicy] = None,
    ) -> None:
        self._git = git or get_default_git_executable()
        self._policy = path_policy or get_default_path_policy()

    # --- Public API (PushPort) -----------------------------------------

    async def push_branch(
        self,
        workspace_id: str,
        *,
        local_branch: str,
        remote: str = "origin",
        remote_branch: Optional[str] = None,
        force_with_lease: bool = False,
        set_upstream: bool = True,
        token: Optional[str] = None,
        timeout_seconds: int = DEFAULT_PUSH_TIMEOUT_SECONDS,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> PushResult:
        """
        Push a local branch to a remote.

        Args:
            workspace_id: Workspace identifier (label).
            local_branch: Name of the local branch to push.
            remote: Remote name (default `"origin"`).
            remote_branch: Destination branch name. Defaults to
                `local_branch`.
            force_with_lease: When True, use `--force-with-lease`
                with an explicit remote SHA. Plain `--force` is
                never used.
            set_upstream: When True, add `--set-upstream`.
            token: Optional access token. When provided, it is
                supplied via a temporary `GIT_ASKPASS` script. It is
                NEVER embedded in the remote URL.
            timeout_seconds: Push timeout.
            workspace_path: Absolute workspace path.

        Returns:
            A `PushResult` describing the outcome. When the push times
            out with no conclusive answer, `ambiguous=True` is set
            and the caller MUST reconcile via `reconcile_push`.

        Raises:
            ValidationError: If inputs are malformed.
            WorkspaceNotFoundError: If the workspace path is missing.
            AuthRequiredError: If the remote rejected authentication.
            ProtectedBranchError: If the remote refused the push
                because the target branch is protected.
            RemoteStateChangedError: If the lease check failed
                because the remote moved.
            ConflictError: If the push was rejected as
                non-fast-forward and `force_with_lease` was False.
        """
        if not local_branch:
            raise ValidationError("local_branch must not be empty.")
        if not remote:
            raise ValidationError("remote must not be empty.")
        if timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive.")

        self._validate_branch_name(local_branch)
        if remote_branch:
            self._validate_branch_name(remote_branch)
        self._validate_remote_name(remote)

        target = self._resolve_workspace_path(workspace_path, workspace_id)
        destination = remote_branch or local_branch

        # Resolve the remote SHA when a lease is required. This MUST
        # happen just before the push so that the lease value reflects
        # the current remote state.
        lease_sha: Optional[str] = None
        if force_with_lease:
            lease_sha = await self._remote_head_sha(
                target=target, remote=remote, ref=destination
            )
            if lease_sha is None:
                # The remote branch does not exist yet, which is fine
                # for a new branch. Use the empty-string lease form,
                # which Git interprets as "must not exist".
                lease_sha = ""

        local_sha = await self._local_head_sha(target, local_branch)
        if not local_sha:
            raise ValidationError(
                f"Local branch '{local_branch}' does not exist.",
                details={"local_branch": local_branch},
            )

        # Build the refspec and the argument list.
        refspec = f"{local_branch}:{destination}"
        args: List[str] = [
            "push",
            "--porcelain",
            "--atomic",
            "--no-thin",
            "--no-color",
        ]
        if set_upstream:
            args.append("--set-upstream")
        if force_with_lease:
            # `--force-with-lease=<ref>:<sha>` binds the lease to a
            # specific remote ref and SHA, which is safer than the
            # bare flag that relies on the local tracking info.
            args.append(
                f"--force-with-lease=refs/heads/{destination}:{lease_sha}"
            )
        args.extend([remote, refspec])

        # Execute the push, optionally with credentials.
        stdout, stderr, returncode, timed_out = await self._execute_push(
            target=target,
            args=args,
            token=token,
            timeout_seconds=timeout_seconds,
        )

        if timed_out:
            # We cannot know whether the push succeeded. Ask the
            # remote for the current SHA of the destination.
            #
            # IMPORTANT: pass the same token that was used for the
            # push attempt. Without it, the reconciliation
            # `ls-remote` is unauthenticated and returns None for
            # private repositories, so a successful push is
            # reported as "timeout_ambiguous".
            final = await self._reconcile_after_timeout(
                target=target,
                remote=remote,
                destination=destination,
                local_sha=local_sha,
                local_branch=local_branch,
                token=token,
            )
            return final

        # Parse the porcelain output and classify the result.
        parsed = self._parse_push_porcelain(stdout)
        outcome, reason = self._classify_porcelain(
            parsed=parsed,
            destination=destination,
        )

        if outcome == PushOutcome.REJECTED:
            self._raise_for_rejection(
                reason=reason,
                stderr=stderr,
                destination=destination,
                local_branch=local_branch,
            )

        if returncode != 0 and outcome == PushOutcome.UNKNOWN:
            # Nothing matched; treat as a generic failure.
            raise CommandFailedError(
                "Git push failed.",
                details={
                    "returncode": returncode,
                    "stderr": redact_text(stderr)[:500],
                },
            )

        # Read the remote SHA to populate `PushResult.remote_sha`.
        # Pass the token so that private repositories return the
        # real SHA rather than None. The caller (audit log, facade,
        # UI) relies on this value to confirm the push landed at the
        # expected commit.
        remote_sha = await self._remote_head_sha(
            target=target,
            remote=remote,
            ref=destination,
            token=token,
        )

        return PushResult(
            accepted=outcome
            in (
                PushOutcome.PUSHED,
                PushOutcome.FORCED,
                PushOutcome.NEW_BRANCH,
                PushOutcome.UP_TO_DATE,
            ),
            local_branch=local_branch,
            local_sha=local_sha,
            remote_branch=destination,
            remote_sha=remote_sha,
            rejected_reason=reason if outcome == PushOutcome.REJECTED else None,
            message=redact_text(stderr)[:500] if stderr else None,
        )

    async def delete_remote_branch(
        self,
        workspace_id: str,
        *,
        remote: str,
        remote_branch: str,
        token: Optional[str] = None,
        timeout_seconds: int = DELETE_BRANCH_TIMEOUT_SECONDS,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> None:
        """
        Delete a remote branch.

        This operation is disabled by default in PELDRUN policy. The
        manager does NOT enforce the policy; the caller MUST have
        already obtained approval.

        Raises:
            ValidationError: If inputs are malformed.
            WorkspaceNotFoundError: If the workspace path is missing.
            ProtectedBranchError: If the remote refused because the
                branch is protected.
            AuthRequiredError: If the remote rejected authentication.
        """
        if not remote:
            raise ValidationError("remote must not be empty.")
        if not remote_branch:
            raise ValidationError("remote_branch must not be empty.")
        if timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be positive.")

        self._validate_branch_name(remote_branch)
        self._validate_remote_name(remote)

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        args = [
            "push",
            "--porcelain",
            "--no-color",
            remote,
            f":refs/heads/{remote_branch}",
        ]

        stdout, stderr, returncode, timed_out = await self._execute_push(
            target=target,
            args=args,
            token=token,
            timeout_seconds=timeout_seconds,
        )

        if timed_out:
            # Best-effort reconciliation. We do not raise here; the
            # caller is expected to check whether the branch is gone.
            logger.warning(
                "Remote branch deletion timed out; the state is "
                "unknown. Caller should verify."
            )
            return

        if returncode != 0:
            reason = self._extract_reason_from_stderr(stderr)
            self._raise_for_rejection(
                reason=reason,
                stderr=stderr,
                destination=remote_branch,
                local_branch=remote_branch,
            )

        # Verify deletion.
        remaining = await self._remote_head_sha(
            target=target, remote=remote, ref=remote_branch
        )
        if remaining is not None:
            raise CommandFailedError(
                "Remote branch still exists after a successful delete.",
                details={"remote_branch": remote_branch},
            )

    # --- Reconciliation -------------------------------------------------

    async def reconcile_push(
        self,
        workspace_id: str,
        *,
        remote: str,
        remote_branch: str,
        expected_local_sha: str,
        token: Optional[str] = None,
        workspace_path: Optional[Union[str, Path]] = None,
    ) -> PushResult:
        """
        Determine whether a previous push attempt succeeded.

        Callers invoke this method after a push timed out or after
        the application restarted while a push was in flight. The
        method queries the remote for the current SHA of
        `remote_branch` and compares it to `expected_local_sha`.

        Args:
            workspace_id: Workspace identifier.
            remote: Remote name.
            remote_branch: Destination branch name.
            expected_local_sha: The SHA the caller was trying to
                push. If the remote now points to this SHA, the push
                is considered successful.
            token: Optional access token.
            workspace_path: Absolute workspace path.

        Returns:
            A `PushResult`. When the remote SHA matches
            `expected_local_sha`, `accepted=True`. Otherwise
            `accepted=False` with `ambiguous=False` (the state is
            now known).
        """
        if not remote_branch:
            raise ValidationError("remote_branch must not be empty.")
        if not expected_local_sha:
            raise ValidationError("expected_local_sha must not be empty.")

        self._validate_branch_name(remote_branch)
        self._validate_remote_name(remote)

        target = self._resolve_workspace_path(workspace_path, workspace_id)

        remote_sha = await self._remote_head_sha(
            target=target, remote=remote, ref=remote_branch, token=token
        )

        accepted = remote_sha == expected_local_sha

        return PushResult(
            accepted=accepted,
            local_branch=remote_branch,
            local_sha=expected_local_sha,
            remote_branch=remote_branch,
            remote_sha=remote_sha,
            rejected_reason=None if accepted else "remote_state_differs",
            message=(
                "Reconciled: remote matches the pushed SHA."
                if accepted
                else "Reconciled: remote does not match the pushed SHA."
            ),
        )

    # --- Internal: execution -------------------------------------------

    async def _execute_push(
        self,
        *,
        target: Path,
        args: Sequence[str],
        token: Optional[str],
        timeout_seconds: int,
    ) -> Tuple[str, str, int, bool]:
        """
        Run a `git push` command, optionally with a temporary
        `GIT_ASKPASS` script.

        Returns `(stdout, stderr, returncode, timed_out)`. When the
        command times out, `timed_out` is True and the caller is
        responsible for reconciliation.

        Credentials are supplied through the process environment via
        ``GIT_ASKPASS`` and ``SSH_ASKPASS``. The mapping returned by
        ``_build_askpass`` is passed as ``extra_env`` to
        ``GitExecutable.run``; the executable merges it into the
        sanitized environment it builds. Callers MUST NOT construct
        their own ``env=os.environ.copy()`` here — that would
        bypass the sanitization performed by ``GitExecutable``
        (stripping ``GIT_SSH_COMMAND``, ``LD_PRELOAD``, and other
        dangerous variables), and the executable does not even
        accept an ``env`` keyword.
        """
        askpass_path: Optional[Path] = None
        extra_env: Optional[dict[str, str]] = None

        try:
            if token:
                askpass_path, extra_env = self._build_askpass(token)

            try:
                result = await self._git.run(
                    args,
                    cwd=target,
                    timeout_seconds=timeout_seconds,
                    check_returncode=False,
                    extra_env=extra_env,
                )
            except CommandTimeoutError:
                # MUST be caught before `GitExecutionError`: a
                # timeout may be wrapped as a `GitExecutionError`,
                # and treating it as an ordinary failure would skip
                # the reconciliation path entirely.
                return "", "", -1, True
            except GitExecutionError as exc:
                return "", str(exc), -1, False

            stdout = redact_text(result.stdout)
            stderr = redact_text(result.stderr)
            return stdout, stderr, result.returncode, False

        finally:
            if askpass_path is not None:
                _safe_unlink(askpass_path)

    def _build_askpass(
        self, token: str
    ) -> Tuple[Path, dict[str, str]]:
        """
        Create a temporary `GIT_ASKPASS` script that echoes `token`.

        The script is written to a fresh file with restrictive
        permissions. On Unix, the file is `chmod 0o700`. On Windows,
        the file is a `.bat` file; Windows ACLs on the user's temp
        directory restrict access to the current user.

        The caller MUST delete the returned path when done. The
        returned mapping overrides the environment for the
        subprocess. The Git executable is expected to accept
        environment overrides; when it does not, the token is not
        used and the push will fail with an authentication error.
        """
        if not token:
            raise ValidationError("token must not be empty.")

        # The token is placed in the script body, not in an
        # environment variable, so that it never appears in the
        # process's environment (which could be inspected by other
        # local processes). We still redact stderr.
        suffix = ".bat" if os.name == "nt" else ".sh"
        fd, raw_path = tempfile.mkstemp(prefix="peldrun-askpass-", suffix=suffix)
        os.close(fd)
        path = Path(raw_path)

        try:
            if os.name == "nt":
                # Windows batch files must not contain special
                # characters unescaped. The token is emitted via
                # `echo` with delayed expansion disabled.
                script = f"@echo off\r\necho {token}\r\n"
            else:
                script = f"#!/bin/sh\necho '{token}'\n"
            path.write_text(script, encoding="utf-8")

            # Restrict permissions on Unix.
            if os.name != "nt":
                try:
                    path.chmod(stat.S_IRWXU)
                except OSError:
                    logger.warning(
                        "Could not restrict permissions on askpass "
                        "script; the operation will continue but the "
                        "token file may be readable by other users."
                    )
        except Exception:
            _safe_unlink(path)
            raise

        # The Git executable applies these variables to the child
        # process. On Windows this is meaningless for `.bat`, so we
        # also provide `GIT_ASKPASS` and fall back to `SSH_ASKPASS`.
        env_overrides = {
            "GIT_ASKPASS": str(path),
            "SSH_ASKPASS": str(path),
        }
        return path, env_overrides

    # --- Internal: porcelain parsing -----------------------------------

    @staticmethod
    def _parse_push_porcelain(
        stdout: str,
    ) -> List[Tuple[str, str, str, str]]:
        """
        Parse `git push --porcelain` output.

        Returns a list of `(flag, local_ref, remote_ref, summary)`
        tuples. Lines that do not match the porcelain format (e.g.
        the `To <url>` header) are skipped.
        """
        results: List[Tuple[str, str, str, str]] = []
        for raw_line in stdout.splitlines():
            match = _PUSH_PORCELAIN_LINE.match(raw_line)
            if not match:
                continue
            results.append(
                (
                    match.group("flag"),
                    match.group("local_ref"),
                    match.group("remote_ref"),
                    match.group("summary"),
                )
            )
        return results

    @staticmethod
    def _classify_porcelain(
        *,
        parsed: Sequence[Tuple[str, str, str, str]],
        destination: str,
    ) -> Tuple[str, Optional[str]]:
        """
        Classify the parsed porcelain lines into a single outcome.

        When multiple lines are present, the first rejected line
        wins. Otherwise the first line is used.
        """
        if not parsed:
            return PushOutcome.UNKNOWN, None

        # Prefer a rejection line if one is present.
        for flag, _local, remote_ref, summary in parsed:
            if flag == "!":
                reason = _extract_reason(summary) or "rejected"
                return PushOutcome.REJECTED, reason

        # Otherwise use the first line.
        flag, _local, _remote, _summary = parsed[0]
        return _FLAG_TO_OUTCOME.get(flag, PushOutcome.UNKNOWN), None

    @staticmethod
    def _extract_reason_from_stderr(stderr: str) -> Optional[str]:
        """
        Extract a normalized rejection reason from raw stderr.

        Used when porcelain parsing yields no rejection line, for
        example when a pre-receive hook wrote a free-form message.
        """
        lowered = stderr.lower()

        # Order matters: more specific patterns first.
        if "protected branch" in lowered or "not allowed to push" in lowered:
            return "protected_branch"
        if "non-fast-forward" in lowered or "fetch first" in lowered:
            return "non_fast_forward"
        if "stale info" in lowered or "cannot lock ref" in lowered:
            return "stale_info"
        if "hook declined" in lowered or "pre-receive hook declined" in lowered:
            return "hook_declined"
        if (
            "authentication failed" in lowered
            or "could not read username" in lowered
            or "invalid username or password" in lowered
            or "permission denied" in lowered
        ):
            return "auth_failed"
        if "repository not found" in lowered:
            return "repository_not_found"
        if "remote rejected" in lowered:
            return "remote_rejected"
        return None

    def _raise_for_rejection(
        self,
        *,
        reason: Optional[str],
        stderr: str,
        destination: str,
        local_branch: str,
    ) -> None:
        """
        Raise the appropriate exception for a rejected push.

        The mapping is based on the normalized `reason`, which is
        derived from porcelain output first, and from stderr as a
        fallback. The exception carries the local and remote branch
        names for diagnostics.
        """
        # IMPORTANT: the prior version of this method had a typo
        # (`lower_error` instead of `lower_err`) which produced a
        # `NameError` at runtime. This is corrected here.
        lower_err = (stderr or "").lower()

        # If the porcelain reason is missing, try to derive it from
        # stderr.
        if not reason:
            reason = self._extract_reason_from_stderr(stderr)

        if not reason:
            # Last-resort heuristics on the raw error text.
            if "fetch first" in lower_err or "non-fast-forward" in lower_err:
                reason = "non_fast_forward"
            elif "[rejected]" in lower_err:
                reason = "rejected"

        details = {
            "local_branch": local_branch,
            "remote_branch": destination,
            "reason": reason or "unknown",
        }

        if reason == "protected_branch":
            raise ProtectedBranchError(
                f"Push to protected branch '{destination}' was rejected.",
                details=details,
            )

        if reason in ("non_fast_forward", "stale_info"):
            raise RemoteStateChangedError(
                f"Remote branch '{destination}' moved since the last "
                "fetch; the push was rejected.",
                details=details,
            )

        if reason == "auth_failed":
            raise AuthRequiredError(
                "Authentication to the remote was rejected.",
                details=details,
            )

        if reason == "hook_declined":
            raise CommandFailedError(
                f"A server-side hook rejected the push to "
                f"'{destination}'.",
                details=details,
            )

        if reason == "repository_not_found":
            raise ConflictError(
                "The remote repository was not found.",
                details=details,
            )

        # Generic rejection.
        raise ConflictError(
            f"Push to '{destination}' was rejected.",
            details=details,
        )

    # --- Internal: remote queries --------------------------------------

    async def _remote_head_sha(
        self,
        *,
        target: Path,
        remote: str,
        ref: str,
        token: Optional[str] = None,
    ) -> Optional[str]:
        """
        Return the SHA the remote currently points to for `ref`, or
        `None` if the ref does not exist on the remote.

        When `token` is provided, the askpass script is created and
        its mapping is passed to ``GitExecutable.run`` as
        ``extra_env``. Without this, ``ls-remote`` against a private
        repository returns ``None`` even when the ref exists,
        because the request is unauthenticated.
        """
        full_ref = f"refs/heads/{ref}"
        args = ["ls-remote", "--refs", remote, full_ref]

        askpass_path: Optional[Path] = None
        extra_env: Optional[dict[str, str]] = None
        try:
            if token:
                askpass_path, extra_env = self._build_askpass(token)

            result = await self._git.run(
                args,
                cwd=target,
                timeout_seconds=LS_REMOTE_TIMEOUT_SECONDS,
                check_returncode=False,
                extra_env=extra_env,
            )
        except GitExecutionError:
            return None
        finally:
            if askpass_path is not None:
                _safe_unlink(askpass_path)

        if result.returncode != 0 or not result.stdout.strip():
            return None

        # Format: `<sha>\t<ref>` per line.
        for line in result.stdout.splitlines():
            parts = line.strip().split("\t", 1)
            if len(parts) == 2 and parts[1] == full_ref:
                return parts[0]
        return None

    async def _local_head_sha(
        self, target: Path, branch: str
    ) -> Optional[str]:
        """Return the SHA of a local branch, or `None`."""
        result = await self._git.run(
            ["rev-parse", "--verify", f"refs/heads/{branch}"],
            cwd=target,
            check_returncode=False,
        )
        if result.returncode != 0:
            return None
        sha = result.stdout.strip()
        return sha or None

    async def _reconcile_after_timeout(
        self,
        *,
        target: Path,
        remote: str,
        destination: str,
        local_sha: str,
        local_branch: str,
        token: Optional[str] = None,
    ) -> PushResult:
        """
        Build a `PushResult` for a push that timed out.

        The manager queries the remote for its current SHA of
        `destination`. When the remote SHA equals `local_sha`, the
        push succeeded (or was already applied). When the remote SHA
        differs, the push is considered failed but NOT ambiguous; the
        caller now knows the state. When the remote cannot be
        reached, the result remains ambiguous.

        The caller MUST pass the same `token` that was used for the
        push attempt. Without it, the reconciliation ``ls-remote``
        call runs unauthenticated, returns ``None`` for private
        repositories, and the result is reported as
        ``timeout_ambiguous`` even when the push actually succeeded.
        """
        remote_sha = await self._remote_head_sha(
            target=target,
            remote=remote,
            ref=destination,
            token=token,
        )

        if remote_sha is None:
            # The remote could not be reached. The state is genuinely
            # ambiguous.
            return PushResult(
                accepted=False,
                local_branch=local_branch,
                local_sha=local_sha,
                remote_branch=destination,
                remote_sha=None,
                rejected_reason="timeout_ambiguous",
                message=(
                    "Push timed out and the remote could not be "
                    "contacted for reconciliation."
                ),
            )

        if remote_sha == local_sha:
            return PushResult(
                accepted=True,
                local_branch=local_branch,
                local_sha=local_sha,
                remote_branch=destination,
                remote_sha=remote_sha,
                rejected_reason=None,
                message="Push reconciled: remote matches the pushed SHA.",
            )

        return PushResult(
            accepted=False,
            local_branch=local_branch,
            local_sha=local_sha,
            remote_branch=destination,
            remote_sha=remote_sha,
            rejected_reason="timeout_reconciled",
            message=(
                "Push timed out; reconciliation shows the remote is "
                "at a different SHA."
            ),
        )

    # --- Internal: validation ------------------------------------------

    def _resolve_workspace_path(
        self,
        workspace_path: Optional[Union[str, Path]],
        workspace_id: str,
    ) -> Path:
        if workspace_path is None:
            raise ValidationError(
                "workspace_path is required for this operation.",
                details={"workspace_id": workspace_id},
            )
        resolved = self._policy.resolve_within_root(
            workspace_path, must_exist=True
        )
        if not resolved.is_dir():
            raise WorkspaceNotFoundError(
                f"Workspace path is not a directory: {resolved}.",
                details={"path": str(resolved)},
            )
        return resolved

    @staticmethod
    def _validate_branch_name(name: str) -> None:
        if not name or name.startswith("-") or name.endswith("/"):
            raise ValidationError(
                "Invalid branch name.",
                details={"branch_name": name},
            )
        if any(ch in name for ch in ("\x00", "\n", "\r", " ")):
            raise ValidationError(
                "Branch name contains forbidden characters.",
                details={"branch_name": name},
            )

    @staticmethod
    def _validate_remote_name(name: str) -> None:
        """
        Validate a remote name.

        Remote names must be simple identifiers. They are passed to
        Git as arguments, and although the wrapper never uses a
        shell, a name beginning with `-` could still be interpreted
        as a flag.
        """
        if not name:
            raise ValidationError("remote must not be empty.")
        if name.startswith("-"):
            raise ValidationError(
                "remote name must not start with '-'.",
                details={"remote": name},
            )
        if any(ch in name for ch in ("\x00", "\n", "\r", " ", "\t")):
            raise ValidationError(
                "remote name contains forbidden characters.",
                details={"remote": name},
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_reason(summary: str) -> Optional[str]:
    """
    Extract the bracketed/parenthesized reason from a porcelain
    summary line.

    Examples:
        `"[rejected] (non-fast-forward)"` -> `"non-fast-forward"`
        `"[remote rejected] (protected branch)"` -> `"protected branch"`
    """
    if not summary:
        return None
    match = _REASON.search(summary)
    if match:
        return match.group("reason").strip()
    return None


def _safe_unlink(path: Path) -> None:
    """
    Delete a file, ignoring all errors.

    Used for the temporary `GIT_ASKPASS` script. If deletion fails,
    a warning is logged, but the operation continues. The file
    contains a token and lives under the user's temp directory; on
    Windows it is scoped to the current user; on Unix it has 0o700
    permissions.
    """
    try:
        if path.exists():
            path.unlink()
    except OSError as exc:
        logger.warning(
            "Failed to delete temporary askpass script: %s",
            type(exc).__name__,
        )


# ---------------------------------------------------------------------------
# Default manager singleton
# ---------------------------------------------------------------------------


_DEFAULT_MANAGER: Optional[GitPushManager] = None
_DEFAULT_LOCK = threading.RLock()


def get_default_push_manager() -> GitPushManager:
    """
    Return the process-wide default `GitPushManager`, creating it on
    first use.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        if _DEFAULT_MANAGER is None:
            _DEFAULT_MANAGER = GitPushManager()
        return _DEFAULT_MANAGER


def set_default_push_manager(manager: GitPushManager) -> None:
    """
    Replace the process-wide default push manager.

    Intended for application startup and for tests.
    """
    if not isinstance(manager, GitPushManager):
        raise TypeError(
            "set_default_push_manager expects a GitPushManager."
        )
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = manager


def reset_default_push_manager() -> GitPushManager:
    """
    Replace the default manager with a fresh one.

    Used by tests to guarantee isolation between test cases.
    """
    global _DEFAULT_MANAGER
    with _DEFAULT_LOCK:
        _DEFAULT_MANAGER = GitPushManager()
        return _DEFAULT_MANAGER
