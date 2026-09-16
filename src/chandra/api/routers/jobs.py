"""Job status, live job state and backend logs.

Third extraction. Unlike the orchestration endpoints these only *read* the
shared runtime — none of them starts work, mutates a job or touches the graph —
so they move with the job store rather than around it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, WebSocket
from fastapi.responses import JSONResponse
from src.chandra.api import runtime
from src.chandra.api.logbuffer import log_buffer

router = APIRouter(tags=["jobs"])


@router.get("/logs")
async def get_logs(
    limit: int = Query(500, ge=1, le=2000), offset: int = Query(0, ge=0)
) -> JSONResponse:
    """Recent backend logs (the in-memory ring buffer, newest last)."""
    entries = log_buffer.read(limit=limit, offset=offset)
    return JSONResponse(status_code=200, content={"logs": entries})


@router.get("/jobs/status/{job_id}")
def get_job_status_generic(job_id: str) -> JSONResponse:
    """Poll the status of any submitted async job."""
    with runtime.job_store_lock:
        if job_id not in runtime.job_store:
            return JSONResponse(
                status_code=404,
                content={
                    "job_id": job_id,
                    "status": "not_found",
                    "message": "No job with this ID exists",
                },
            )
        job: dict[str, Any] = dict(runtime.job_store[job_id])
    return JSONResponse(status_code=200, content={"job_id": job_id, **job})


@router.websocket("/ws/jobs/{job_id}")
async def job_state_socket(websocket: WebSocket, job_id: str) -> None:
    """Live job state.

    Read-only by design: this is a status feed, not a command channel. Accepting
    commands over a socket would route around the authenticated, rate-limited,
    RBAC-checked HTTP edge. The receive loop exists only to notice a disconnect.
    """
    with runtime.job_store_lock:
        snapshot = dict(runtime.job_store.get(job_id, {}))
    await runtime.ws_manager.subscribe(job_id, websocket)
    try:
        if snapshot:
            snapshot.pop("result", None)
            await websocket.send_json({"job_id": job_id, **snapshot})
        else:
            await websocket.send_json({"job_id": job_id, "status": "not_found"})
        while True:
            await websocket.receive_text()
    except Exception:
        pass
    finally:
        await runtime.ws_manager.unsubscribe(job_id, websocket)
