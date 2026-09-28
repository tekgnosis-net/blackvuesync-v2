"""APScheduler integration: cron-triggered sync inside the long-running web service."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.sync_runner import trigger_sync
from blackvuesync_v2.settings import (
    DEFAULT_CRON_EXPRESSION,
    DEFAULT_TIMEZONE,
    Settings,
    SettingsStore,
    cron_trigger_fields,
)

if TYPE_CHECKING:
    from blackvuesync_v2.server.stats_store import StatsStore

logger = logging.getLogger(__name__)

# the single job id; reused by reschedule_job and remove_job.
_JOB_ID = "sync"


def build_cron_trigger(expression: str, timezone: str) -> BaseTrigger:
    """builds a trigger that follows standard cron semantics.

    raises ValueError (or an apscheduler/zoneinfo error) if the expression or
    timezone is invalid.
    """
    triggers = [
        CronTrigger(timezone=timezone, **kwargs)
        for kwargs in cron_trigger_fields(expression)
    ]
    return triggers[0] if len(triggers) == 1 else OrTrigger(triggers)


def _build_trigger(settings: Settings) -> BaseTrigger:
    """builds the sync trigger from settings, falling back to the default.

    a stored schedule that the scheduler rejects (e.g. written by an older
    release with looser validation) must not crash the service on startup.
    """
    expression = settings.schedule.cron_expression
    timezone = settings.schedule.timezone
    try:
        return build_cron_trigger(expression, timezone)
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception(
            "invalid schedule %r (%s); falling back to %r (%s)",
            expression,
            timezone,
            DEFAULT_CRON_EXPRESSION,
            DEFAULT_TIMEZONE,
        )
        return build_cron_trigger(DEFAULT_CRON_EXPRESSION, DEFAULT_TIMEZONE)


def _scheduled_run(
    store: SettingsStore,
    publisher: ProgressPublisher,
    stats_store: StatsStore | None = None,
) -> None:
    """job function: triggers a sync via the shared trigger_sync entrypoint.

    settings are read fresh on each tick so updates to e.g. address, timeout,
    or schedule.paused apply on the next scheduled run without a restart.
    """
    settings = store.get()
    if settings.schedule.paused:
        logger.info("scheduled sync skipped: schedule is paused")
        return
    result = trigger_sync(settings, publisher, stats_store)
    if result["status"] == "already_running":
        logger.info(
            "scheduled sync skipped: another sync is already running (job_id=%s)",
            result["job_id"],
        )


def init_scheduler(
    store: SettingsStore,
    publisher: ProgressPublisher,
    stats_store: StatsStore | None = None,
) -> BackgroundScheduler:
    """initializes and starts a BackgroundScheduler with one cron-triggered job.

    the scheduler uses a single-thread executor so concurrent fires (e.g. on
    schedule transitions) cannot overlap. `max_instances=1` and
    `coalesce=True` further enforce that a backlog of missed runs collapses
    to one. the cron expression and timezone are read from settings at init,
    and a SettingsStore on_change listener reschedules the job in-place when
    those fields change.
    """
    # the job trigger carries its own timezone, so the scheduler default is
    # UTC rather than a stored timezone that might be invalid.
    scheduler = BackgroundScheduler(
        executors={"default": ThreadPoolExecutor(max_workers=1)},
        timezone=DEFAULT_TIMEZONE,
    )
    scheduler.add_job(
        _scheduled_run,
        trigger=_build_trigger(store.get()),
        id=_JOB_ID,
        args=(store, publisher, stats_store),
        max_instances=1,
        coalesce=True,
        replace_existing=True,
    )

    def _on_change(old: Settings, new: Settings) -> None:
        """reschedules the sync job when schedule settings change."""
        if old.schedule == new.schedule:
            return
        logger.info(
            "rescheduling sync job: %r/%s -> %r/%s",
            old.schedule.cron_expression,
            old.schedule.timezone,
            new.schedule.cron_expression,
            new.schedule.timezone,
        )
        scheduler.reschedule_job(_JOB_ID, trigger=_build_trigger(new))

    store.on_change(_on_change)
    scheduler.start()
    return scheduler


__all__ = ["build_cron_trigger", "init_scheduler"]
