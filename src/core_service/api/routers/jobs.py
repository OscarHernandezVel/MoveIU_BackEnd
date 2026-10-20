"""Periodic jobs driven from outside, for deployments without an always-on process (serverless).

On a server the :class:`JobRunner` thread calls ``run_pending`` once per second. Where there is no thread
(Vercel Functions), a scheduler calls this endpoint instead - Vercel Cron, or a tiny worker container - and the
same lease mechanism makes every job run once per interval no matter how many callers or replicas there are.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from core_service.api.dependencies import get_context, require_cron_secret
from core_service.container import ApplicationContext

router = APIRouter(prefix="/internal", tags=["jobs"], dependencies=[Depends(require_cron_secret)])


@router.api_route("/jobs/run", methods=["GET", "POST"], summary="Run the jobs that are due")
def run_due_jobs(context: ApplicationContext = Depends(get_context)) -> dict[str, Any]:
    return {"executed": context.jobs.run_pending()}
