"""Standalone schedules: cron parsing, the host-side book, firing, wire ops."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from tangram_app.local_actions import LocalActionsServer
from tangram_app.local_schedules import (
    AUTOPAUSE_AFTER,
    Cron,
    LocalScheduler,
    ScheduleError,
    parse_at,
    parse_every,
)

UTC = timezone.utc


class _Effect:
    def __init__(self, value):
        self.value = value


def _action(resource_type, name, effect="Stateless", requires_confirmation=False):
    return SimpleNamespace(
        id=f"com.example/demo#{resource_type}.{name}@op{name}",
        resource_type=resource_type,
        name=name,
        effect=_Effect(effect),
        requires_confirmation=requires_confirmation,
    )


def _session(actions):
    app = SimpleNamespace(graph=SimpleNamespace(actions=actions))
    return SimpleNamespace(app=app)


class CadenceParsingTest(unittest.TestCase):
    def test_every(self):
        self.assertEqual(parse_every("30m"), 1800)
        self.assertEqual(parse_every("2h"), 7200)
        self.assertEqual(parse_every("90s"), 90)
        self.assertEqual(parse_every("1d"), 86400)
        for bad in ("0m", "-5m", "m", "30", "30w", "*/30"):
            with self.assertRaises(ScheduleError):
                parse_every(bad)

    def test_at_requires_an_aware_future_style_instant(self):
        moment = parse_at("2030-01-02T03:04:05+00:00")
        self.assertEqual(moment, datetime(2030, 1, 2, 3, 4, 5, tzinfo=UTC))
        parse_at("2030-01-02T03:04:05Z")
        for bad in ("2030-01-02T03:04:05", "not-a-date", None):
            with self.assertRaises(ScheduleError):
                parse_at(bad)

    def test_cron_next_after(self):
        base = datetime(2026, 8, 29, 10, 30, tzinfo=UTC)  # a Saturday
        tz = ZoneInfo("UTC")
        cases = [
            ("*/15 * * * *", datetime(2026, 8, 29, 10, 45, tzinfo=UTC)),
            ("0 9 * * *", datetime(2026, 8, 30, 9, 0, tzinfo=UTC)),
            ("30 10 * * *", datetime(2026, 8, 30, 10, 30, tzinfo=UTC)),  # strictly after
            ("0 0 1 * *", datetime(2026, 9, 1, 0, 0, tzinfo=UTC)),
            ("0 12 * * 1", datetime(2026, 8, 31, 12, 0, tzinfo=UTC)),  # Monday
            ("0 12 * * 0", datetime(2026, 8, 30, 12, 0, tzinfo=UTC)),  # Sunday as 0
            ("0 12 * * 7", datetime(2026, 8, 30, 12, 0, tzinfo=UTC)),  # Sunday as 7
            ("5 4 29 2 *", datetime(2028, 2, 29, 4, 5, tzinfo=UTC)),  # leap day
        ]
        for expression, expected in cases:
            self.assertEqual(Cron(expression).next_after(base, tz), expected, expression)

    def test_cron_dom_dow_or_semantics(self):
        # both restricted → fires on the 15th OR on Mondays
        cron = Cron("0 0 15 * 1")
        after = cron.next_after(datetime(2026, 8, 29, 0, 0, tzinfo=UTC), ZoneInfo("UTC"))
        self.assertEqual(after, datetime(2026, 8, 31, 0, 0, tzinfo=UTC))  # Monday before the 15th

    def test_cron_timezone_evaluation(self):
        cron = Cron("0 9 * * *")
        base = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
        fire = cron.next_after(base, ZoneInfo("America/New_York"))
        self.assertEqual(fire, datetime(2026, 8, 29, 13, 0, tzinfo=UTC))  # 9am EDT same day

    def test_cron_refuses_quartz_and_bad_fields(self):
        for bad in ("0 0 * * * *", "* * * *", "0 0 ? * MON", "*/70 * * * *", "61 * * * *", "0 0 32 * *"):
            with self.assertRaises(ScheduleError):
                Cron(bad)


class _Book:
    """A scheduler with injected clock + invoker over a temp state file."""

    def __init__(self, actions=None, fail=False):
        self.now = datetime(2026, 8, 29, 10, 0, tzinfo=UTC)
        self.fired = []
        self.fail = fail
        self.state_path = Path(tempfile.mkdtemp()) / "schedules.json"
        self.scheduler = LocalScheduler(
            self.state_path, invoker=self._invoke, now_fn=lambda: self.now
        )
        self.scheduler._session = _session(actions if actions is not None else [_action("Todo", "Sweep")])

    def _invoke(self, session, resource_type, action, args):
        self.fired.append((resource_type, action, args))
        if self.fail:
            raise RuntimeError("action blew up")


class LocalSchedulerTest(unittest.TestCase):
    def test_create_validates_and_upserts_by_name(self):
        book = _Book()
        schedule = book.scheduler.handle(
            "create",
            {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "every": "30m"},
        )["schedule"]
        self.assertEqual(schedule["state"], "active")
        self.assertEqual(schedule["next_fire"], book.now.isoformat())  # first fire immediately
        updated = book.scheduler.handle(
            "create",
            {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "cron": "0 9 * * *"},
        )["schedule"]
        self.assertEqual(updated["cron"], "0 9 * * *")
        self.assertNotIn("every", updated)
        self.assertEqual(len(book.scheduler.handle("list", {})["schedules"]), 1)

    def test_create_refuses_bad_requests(self):
        book = _Book(actions=[_action("Todo", "Sweep"), _action("Todo", "Purge", effect="Irreversible")])
        base = {"name": "x", "resource_type": "Todo", "action": "Sweep"}
        bad_requests = [
            {**base, "every": "30m", "cron": "* * * * *"},  # two cadences
            {**base},  # no cadence
            {**base, "every": "30m", "name": "Bad Name"},
            {**base, "every": "30m", "timezone": "Mars/Olympus"},
            {**base, "every": "30m", "args": "not-an-object"},
            {"name": "x", "resource_type": "Todo", "action": "Purge", "every": "30m"},  # gated
            {"name": "x", "resource_type": "Ghost", "action": "Sweep", "every": "30m"},
            {**base, "at": "2020-01-01T00:00:00Z"},  # past
            {**base, "args": {"blob": "x" * 40000}, "every": "30m"},  # over cap
        ]
        for body in bad_requests:
            with self.assertRaises(ScheduleError, msg=body):
                book.scheduler.handle("create", body)

    def test_tick_fires_due_schedules_and_advances(self):
        book = _Book()
        book.scheduler.handle(
            "create",
            {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "args": {"n": 1}, "every": "30m"},
        )
        book.scheduler.tick(book.now)
        self.assertEqual(book.fired, [("Todo", "Sweep", {"n": 1})])
        book.scheduler.tick(book.now)  # not due again yet
        self.assertEqual(len(book.fired), 1)
        book.now += timedelta(minutes=30)
        book.scheduler.tick(book.now)
        self.assertEqual(len(book.fired), 2)
        runs = book.scheduler.handle("runs", {"name": "sweep"})["runs"]
        self.assertEqual([run["status"] for run in runs], ["succeeded", "succeeded"])
        self.assertTrue(all(run["id"].startswith("schedule-run-") for run in runs))

    def test_at_schedule_fires_once_and_completes(self):
        book = _Book()
        at = (book.now + timedelta(hours=1)).isoformat()
        book.scheduler.handle(
            "create", {"name": "once", "resource_type": "Todo", "action": "Sweep", "at": at}
        )
        book.now += timedelta(hours=2)
        book.scheduler.tick(book.now)
        book.scheduler.tick(book.now)
        self.assertEqual(len(book.fired), 1)
        listed = book.scheduler.handle("list", {})["schedules"][0]
        self.assertEqual(listed["state"], "completed")
        with self.assertRaises(ScheduleError):
            book.scheduler.handle("resume", {"name": "once"})

    def test_consecutive_failures_autopause_and_resume_rearms(self):
        book = _Book(fail=True)
        book.scheduler.handle(
            "create", {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "every": "1m"}
        )
        for _ in range(AUTOPAUSE_AFTER):
            book.scheduler.tick(book.now)
            book.now += timedelta(minutes=1)
        listed = book.scheduler.handle("list", {})["schedules"][0]
        self.assertEqual(listed["state"], "autopaused")
        self.assertEqual(listed["consecutive_failures"], AUTOPAUSE_AFTER)
        book.scheduler.tick(book.now)
        self.assertEqual(len(book.fired), AUTOPAUSE_AFTER)  # paused: no more fires
        book.fail = False
        resumed = book.scheduler.handle("resume", {"name": "sweep"})["resumed"]
        self.assertEqual(resumed["consecutive_failures"], 0)
        book.scheduler.tick(book.now)
        self.assertEqual(len(book.fired), AUTOPAUSE_AFTER + 1)

    def test_pause_delete_and_missing_names(self):
        book = _Book()
        book.scheduler.handle(
            "create", {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "every": "1h"}
        )
        book.scheduler.handle("pause", {"name": "sweep"})
        book.scheduler.tick(book.now)
        self.assertEqual(book.fired, [])
        book.scheduler.handle("delete", {"name": "sweep"})
        for op in ("delete", "pause", "resume", "runs"):
            with self.assertRaises(ScheduleError) as caught:
                book.scheduler.handle(op, {"name": "sweep"})
            self.assertEqual(caught.exception.status, 404)

    def test_missed_windows_collapse_into_one_fire(self):
        book = _Book()
        book.scheduler.handle(
            "create", {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "every": "1m"}
        )
        book.now += timedelta(hours=3)  # "host was down"
        book.scheduler.tick(book.now)
        book.scheduler.tick(book.now)
        self.assertEqual(len(book.fired), 1)

    def test_state_survives_a_scheduler_restart(self):
        book = _Book()
        book.scheduler.handle(
            "create", {"name": "sweep", "resource_type": "Todo", "action": "Sweep", "every": "1h"}
        )
        book.scheduler.tick(book.now)
        reloaded = LocalScheduler(book.state_path, invoker=book._invoke, now_fn=lambda: book.now)
        reloaded._session = book.scheduler._session
        listed = reloaded.handle("list", {})["schedules"]
        self.assertEqual(listed[0]["name"], "sweep")
        self.assertEqual(len(reloaded.handle("runs", {"name": "sweep"})["runs"]), 1)


class ScheduleWireTest(unittest.TestCase):
    """The /schedules/* ops through the loopback server and staged module."""

    def _load_staged_module(self):
        source = Path(__file__).resolve().parents[1] / "src" / "tangram_app" / "backend_runtime_sdk.py"
        spec = importlib.util.spec_from_file_location("staged_tangram_schedules", source)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def setUp(self):
        self.book = _Book()
        self.server = LocalActionsServer.start(scheduler=self.book.scheduler)
        self.addCleanup(self.server.close)
        self.server.attach(_session([_action("Todo", "Sweep")]))
        self.module = self._load_staged_module()
        os.environ["TANGRAM_LOCAL_ACTIONS_URL"] = self.server.url
        os.environ["TANGRAM_LOCAL_ACTIONS_TOKEN"] = self.server.token
        self.addCleanup(os.environ.pop, "TANGRAM_LOCAL_ACTIONS_URL", None)
        self.addCleanup(os.environ.pop, "TANGRAM_LOCAL_ACTIONS_TOKEN", None)

    def test_module_round_trip_with_platform_signatures(self):
        # cron keeps next_fire in the future so the live thread stays idle
        schedule = self.module.schedules.create("sweep", "Todo", "Sweep", cron="0 9 * * *")
        self.assertEqual(schedule["name"], "sweep")
        self.assertEqual(len(self.module.schedules.list()), 1)
        self.assertEqual(self.module.schedules.pause("sweep")["state"], "paused")
        self.assertEqual(self.module.schedules.resume("sweep")["state"], "active")
        self.assertEqual(self.module.schedules.runs("sweep"), [])
        self.assertEqual(self.module.schedules.delete("sweep")["name"], "sweep")

    def test_module_surfaces_structured_errors(self):
        with self.assertRaises(self.module.ActionError) as caught:
            self.module.schedules.delete("ghost")
        self.assertEqual(caught.exception.code, "not_found")
        with self.assertRaises(self.module.ActionError) as caught:
            self.module.schedules.create("x", "Todo", "Sweep", cron="bad", every="30m")
        self.assertEqual(caught.exception.code, "invalid_request")

    def test_unauthenticated_and_unknown_ops_refuse(self):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            f"{self.server.url}/schedules/list", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=5)
        self.assertEqual(caught.exception.code, 401)
        with self.assertRaises(self.module.ActionError) as unknown:
            self.module._local_host_call("schedules", "schedules/nonsense", {})
        self.assertEqual(unknown.exception.code, "not_found")


if __name__ == "__main__":
    unittest.main()
