"""Database-owned push schedule with one shared interpretation.

Adapters submit schedule intent (friendly times or cron text).  This module owns
normalization, validation, persistence, display, scheduler registration, and
minimum-interval interpretation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Protocol, Sequence

from apscheduler.triggers.cron import CronTrigger


DEFAULT_PUSH_SCHEDULE = "30 9 * * *,0 21 * * *"
PUSH_SCHEDULE_STATE_KEY = "schedule_cron"


class PushScheduleState(Protocol):
    async def read(self) -> str | None: ...

    async def write(self, value: str) -> None: ...


class DatabasePushScheduleState:
    """Production adapter for the database-owned schedule state."""

    async def read(self) -> str | None:
        import database

        return await database.get_state(PUSH_SCHEDULE_STATE_KEY)

    async def write(self, value: str) -> None:
        import database

        await database.set_state(PUSH_SCHEDULE_STATE_KEY, value)


def _cron_parts(value: str) -> tuple[str, ...]:
    candidate = str(value or "").strip()
    if not candidate:
        return ()
    try:
        CronTrigger.from_crontab(candidate)
        return (candidate,)
    except ValueError:
        parts = tuple(part.strip() for part in candidate.split(",") if part.strip())
        if not parts:
            return ()
        for part in parts:
            CronTrigger.from_crontab(part)
        return parts


def _friendly_times(value: str) -> tuple[str, ...] | None:
    compact = "".join(str(value or "").split())
    if compact.isdigit():
        hour = int(compact)
        if not 0 <= hour <= 23:
            raise ValueError(f"无效的推送时间: {compact}")
        return (f"0 {hour} * * *",)
    if not compact or ":" not in compact:
        return None
    times = compact.split(",")
    normalized = []
    for value in times:
        pieces = value.split(":")
        if len(pieces) != 2 or not all(piece.isdigit() for piece in pieces):
            return None
        hour, minute = (int(piece) for piece in pieces)
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError(f"无效的推送时间: {value}")
        normalized.append(f"{minute} {hour} * * *")
    return tuple(normalized)


@dataclass(frozen=True)
class PushSchedule:
    """Validated push times plus all domain interpretations callers need."""

    cron_expressions: tuple[str, ...]

    @classmethod
    def from_intent(cls, value: str | "PushSchedule") -> "PushSchedule":
        if isinstance(value, cls):
            return value
        friendly = _friendly_times(value)
        expressions = friendly if friendly is not None else _cron_parts(value)
        if not expressions:
            raise ValueError("推送计划不能为空")
        return cls(expressions)

    @classmethod
    def default(cls) -> "PushSchedule":
        return cls.from_intent(DEFAULT_PUSH_SCHEDULE)

    @property
    def serialized(self) -> str:
        return ",".join(self.cron_expressions)

    def with_added(self, intent: str | "PushSchedule") -> "PushSchedule":
        additional = PushSchedule.from_intent(intent)
        return PushSchedule((*self.cron_expressions, *additional.cron_expressions))

    @property
    def description(self) -> str:
        return "; ".join(self._describe_cron(value) for value in self.cron_expressions)

    @staticmethod
    def _describe_cron(value: str) -> str:
        minute, hour, day, month, weekday = value.split()
        if day == month == weekday == "*":
            if minute == "0" and hour == "*":
                return "每小时整点"
            if minute == "0" and hour.startswith("*/"):
                return f"每{hour[2:]}小时整点"
            if "," in hour:
                return f"每天 {', '.join(f'{item}:{minute.zfill(2)}' for item in hour.split(','))}"
            if hour.isdigit() and minute.isdigit():
                return f"每天 {int(hour)}:{minute.zfill(2)}"
        return value

    def minimum_interval(self, now: datetime | None = None) -> timedelta:
        """Estimate the shortest interval, preserving the safe four-hour fallback."""
        fallback = timedelta(hours=4)
        fire_times = []
        current = now or datetime.now()
        for cron_expression in self.cron_expressions:
            trigger = CronTrigger.from_crontab(cron_expression)
            previous_fire_time = None
            cursor = current
            for _ in range(8):
                next_fire_time = trigger.get_next_fire_time(previous_fire_time, cursor)
                if next_fire_time is None:
                    break
                fire_time = (
                    next_fire_time.replace(tzinfo=None)
                    if next_fire_time.tzinfo
                    else next_fire_time
                )
                fire_times.append(fire_time)
                previous_fire_time = next_fire_time
                cursor = next_fire_time
        unique_fire_times = sorted(set(fire_times))
        intervals = [
            later - earlier
            for earlier, later in zip(unique_fire_times, unique_fire_times[1:])
            if later > earlier
        ]
        return min(intervals) if intervals else fallback

    def install(
        self,
        scheduler,
        callback: Callable[..., Awaitable[object]],
        args: Sequence[object],
        *,
        replace: bool = False,
        coalesce: bool = True,
    ) -> None:
        """Install the schedule through an APScheduler-compatible adapter."""
        if replace:
            for job in scheduler.get_jobs():
                if job.id.startswith("push_job"):
                    scheduler.remove_job(job.id)
        for index, cron_expression in enumerate(self.cron_expressions):
            scheduler.add_job(
                callback,
                CronTrigger.from_crontab(cron_expression),
                args=list(args),
                id=f"push_job_{index}",
                coalesce=coalesce,
                misfire_grace_time=3600,
            )


class PushScheduleModule:
    """Small external interface for the database-owned Push Schedule."""

    def __init__(self, state: PushScheduleState):
        self._state = state

    async def get(self) -> PushSchedule:
        raw_value = str(await self._state.read() or "").strip()
        if raw_value:
            try:
                return PushSchedule.from_intent(raw_value)
            except ValueError:
                pass
        schedule = PushSchedule.default()
        await self._state.write(schedule.serialized)
        return schedule

    async def update(self, intent: str | PushSchedule) -> PushSchedule:
        schedule = PushSchedule.from_intent(intent)
        await self._state.write(schedule.serialized)
        return schedule
