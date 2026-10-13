"""
Run router package.

Aggregates every endpoint submodule into a single :class:`APIRouter` that
the application can mount. Each submodule owns a cohesive slice of the
run lifecycle:

* :mod:`start_run`   - job creation.
* :mod:`jobs`        - job inspection.
* :mod:`streaming`   - SSE event stream.
* :mod:`files`       - workspace file listing and text I/O.
* :mod:`raw_files`   - raw file delivery and ZIP download.
* :mod:`control`     - stop/cancel.
* :mod:`human_input` - human-in-the-loop responses.

Shared runtime state lives in :mod:`state`, request schemas in
:mod:`schemas`, and reusable helpers in :mod:`utils`.

Implementation note:
    :mod:`start_run` declares an empty-path route (``@router.post("")``)
    alongside its ``"/"`` variant so that the mounted endpoint resolves
    both with and without a trailing slash. FastAPI's ``include_router``
    rejects an empty prefix combined with an empty path, so the routes
    from :mod:`start_run` are attached to the aggregate router directly
    rather than via ``include_router``.
"""

from __future__ import annotations

from fastapi import APIRouter

from . import (
    control,
    files,
    human_input,
    jobs,
    raw_files,
    start_run,
    streaming,
)


# Single aggregated router mounted by the application.
router = APIRouter()

# Attach the start_run routes directly: its empty-path route cannot be
# included through include_router without a non-empty prefix.
router.routes.extend(start_run.router.routes)

# All remaining submodules declare non-empty paths and can be included
# normally. Order does not affect routing; FastAPI matches by path.
router.include_router(jobs.router)
router.include_router(streaming.router)
router.include_router(files.router)
router.include_router(raw_files.router)
router.include_router(control.router)
router.include_router(human_input.router)


__all__ = ["router"]
