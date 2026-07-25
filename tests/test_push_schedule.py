import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from push_schedule import PushSchedule, PushScheduleModule


class MemoryScheduleState:
    def __init__(self, value=None):
        self.value = value
        self.writes = []

    async def read(self):
        return self.value

    async def write(self, value):
        self.value = value
        self.writes.append(value)


class PushScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def test_module_owns_default_initialization_and_friendly_update(self):
        state = MemoryScheduleState()
        module = PushScheduleModule(state)

        initial = await module.get()
        updated = await module.update("9:30,21:00")

        self.assertEqual(initial.description, "每天 9:30; 每天 21:00")
        self.assertEqual(updated.serialized, "30 9 * * *,0 21 * * *")
        self.assertEqual(state.writes[-1], updated.serialized)

    async def test_invalid_stored_value_is_replaced_by_safe_default(self):
        state = MemoryScheduleState("not a cron")

        schedule = await PushScheduleModule(state).get()

        self.assertEqual(schedule, PushSchedule.default())
        self.assertEqual(state.writes, [PushSchedule.default().serialized])

    async def test_schedule_interprets_interval_and_installs_jobs(self):
        schedule = PushSchedule.from_intent("0 */3 * * *")
        scheduler = MagicMock()
        scheduler.get_jobs.return_value = [
            SimpleNamespace(id="push_job_0"),
            SimpleNamespace(id="daily_report_job"),
        ]

        schedule.install(
            scheduler,
            MagicMock(),
            ["config"],
            replace=True,
        )

        scheduler.remove_job.assert_called_once_with("push_job_0")
        scheduler.add_job.assert_called_once()
        self.assertEqual(
            schedule.minimum_interval(datetime(2026, 7, 25, 0, 0)),
            timedelta(hours=3),
        )

    def test_invalid_time_or_cron_is_rejected(self):
        for value in ("24:00", "9:60", "not a cron", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                PushSchedule.from_intent(value)


if __name__ == "__main__":
    unittest.main()
