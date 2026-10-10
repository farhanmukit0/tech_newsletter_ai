"""In-process scheduler. Run a single app instance with it enabled (or use cron + the CLI)."""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .config import settings
from .pipeline import run_issue

log = logging.getLogger(__name__)


def _job(cadence: str) -> None:
    try:
        run_issue(cadence)
    except Exception:
        log.exception("scheduled %s issue failed", cadence)


def start_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone=settings.timezone)
    scheduler.add_job(
        _job, CronTrigger(hour=settings.daily_send_hour, minute=settings.daily_send_minute),
        args=["daily"], id="daily", max_instances=1, coalesce=True, misfire_grace_time=3 * 3600,
    )
    scheduler.add_job(
        _job, CronTrigger(day_of_week=settings.weekly_send_day, hour=settings.weekly_send_hour, minute=0),
        args=["weekly"], id="weekly", max_instances=1, coalesce=True, misfire_grace_time=6 * 3600,
    )
    scheduler.start()
    for job in scheduler.get_jobs():
        log.info("scheduled %s issue, next run %s", job.id, job.next_run_time)
    return scheduler
