"""Standalone durable schedules for locally hosted backends (§5.5 parity).

The scheduler lives in the LOCAL HOST — platform parity: the OS scheduler,
never app code — and fires the app's OWN unattended-eligible actions through
the same governed loopback pipeline as ``tangram.actions.invoke``. Deliberate
standalone divergences (documented in the ABI): firing happens only while a
run session is up (missed windows collapse into one fire), cron is standard
5-field Unix only (Quartz refused), and the scheduling capability is treated
as granted (capability report: emulated). State persists across sessions in
``.preview/schedules.json``.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from zoneinfo import ZoneInfo

if TYPE_CHECKING:  # pragma: no cover
    from .local_runtime import LocalAppSession

AUTOPAUSE_AFTER = 5
RUNS_KEPT = 20
MAX_ARGS_BYTES = 32 * 1024
TICK_SECONDS = 1.0
_CRON_SEARCH_DAYS = 366 * 4

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_EVERY = re.compile(r"^([1-9]\d*)([smhd])$")
_EVERY_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_FIELD_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))


class ScheduleError(Exception):
    """A schedule-surface failure with an ABI error code and HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


def parse_every(text: str) -> int:
    match = _EVERY.match(text) if isinstance(text, str) else None
    if not match:
        raise ScheduleError(
            "invalid_request", f"every must be <n><s|m|h|d> like '30m', got {text!r}"
        )
    return int(match.group(1)) * _EVERY_SECONDS[match.group(2)]


def parse_at(text: str) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError) as error:
        raise ScheduleError("invalid_request", f"at must be an ISO-8601 instant: {error}") from None
    if moment.tzinfo is None:
        raise ScheduleError("invalid_request", "at must carry a UTC offset or Z")
    return moment.astimezone(timezone.utc)


def _parse_field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for atom in text.split(","):
        step = 1
        if "/" in atom:
            atom, step_text = atom.split("/", 1)
            if not step_text.isdigit() or not 1 <= int(step_text) <= high - low + 1:
                raise ValueError(f"bad step {step_text!r}")
            step = int(step_text)
        if atom == "*":
            start, stop = low, high
        elif "-" in atom:
            start_text, stop_text = atom.split("-", 1)
            start, stop = int(start_text), int(stop_text)
        elif atom.isdigit():
            start = stop = int(atom)
        else:
            raise ValueError(f"bad atom {atom!r}")
        if start < low or stop > high or start > stop:
            raise ValueError(f"{atom!r} outside {low}-{high}")
        values.update(range(start, stop + 1, step))
    return frozenset(values)


class Cron:
    """Standard 5-field Unix cron (numeric atoms: ``* , - /``).

    Day-of-month and day-of-week combine with OR when both are restricted,
    per classic cron. Quartz (6/7 fields, names, ``?``/``L``) is refused —
    the platform accepts it, standalone deliberately does not."""

    def __init__(self, expression: str):
        fields = expression.split()
        if len(fields) != 5:
            raise ScheduleError(
                "invalid_request",
                f"cron must be standard 5-field Unix (got {len(fields)} fields); "
                "Quartz is not supported by the standalone host",
            )
        try:
            parsed = [
                _parse_field(field, low, high)
                for field, (low, high) in zip(fields, _FIELD_RANGES)
            ]
        except ValueError as error:
            raise ScheduleError("invalid_request", f"invalid cron {expression!r}: {error}") from None
        self.minutes, self.hours, self.doms, self.months, dows = parsed
        self.dows = frozenset(0 if v == 7 else v for v in dows)
        self.dom_star = fields[2] == "*"
        self.dow_star = fields[4] == "*"

    def _day_matches(self, day) -> bool:
        cron_dow = (day.weekday() + 1) % 7  # cron: Sunday = 0
        dom_ok, dow_ok = day.day in self.doms, cron_dow in self.dows
        if self.dom_star and self.dow_star:
            return True
        if self.dom_star:
            return dow_ok
        if self.dow_star:
            return dom_ok
        return dom_ok or dow_ok  # classic cron OR when both are restricted

    def next_after(self, moment: datetime, tz: ZoneInfo) -> datetime:
        """First matching instant strictly after `moment`, returned in UTC."""
        local = moment.astimezone(tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
        hours, minutes = sorted(self.hours), sorted(self.minutes)
        for offset in range(_CRON_SEARCH_DAYS):
            day = (local + timedelta(days=offset)).date() if offset else local.date()
            if day.month not in self.months or not self._day_matches(day):
                continue
            floor = (local.hour, local.minute) if offset == 0 else (0, 0)
            for hour in hours:
                if hour < floor[0]:
                    continue
                for minute in minutes:
                    if hour == floor[0] and minute < floor[1]:
                        continue
                    candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
                    return candidate.astimezone(timezone.utc)
        raise ScheduleError("invalid_request", "cron expression never matches a real date")


def _validate_target(session, resource_type: str, action_name: str) -> None:
    matches = [
        action
        for action in session.app.graph.actions
        if action.resource_type == resource_type and action.name == action_name
    ]
    if len(matches) != 1:
        raise ScheduleError(
            "invalid_request",
            f"app declares no unambiguous action {resource_type}.{action_name}",
        )
    if matches[0].effect.value == "Irreversible" or matches[0].requires_confirmation:
        raise ScheduleError(
            "invalid_request",
            f"schedules may only target unattended-eligible actions; "
            f"'{resource_type}.{action_name}' needs user confirmation",
        )


class LocalScheduler:
    """Schedule book + firing thread for one local host.

    `handle()` serves the wire ops; `tick()` fires due schedules (kept
    separate and clock-injectable for tests). All mutations persist
    atomically to the state file."""

    def __init__(
        self,
        state_path: str | Path,
        invoker: Callable[..., Any] | None = None,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._invoker = invoker
        self._now = now_fn or (lambda: datetime.now(timezone.utc))
        self._lock = threading.RLock()
        self._session: "LocalAppSession | None" = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._schedules: dict[str, dict] = {}
        self._counter = 0
        self._load()

    # -- lifecycle ---------------------------------------------------------

    def attach(self, session: "LocalAppSession") -> None:
        with self._lock:
            self._session = session
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.tick(self._now())
            except Exception:  # a broken tick must not kill the loop
                pass

    # -- persistence -------------------------------------------------------

    def _load(self) -> None:
        try:
            state = json.loads(self._path.read_text(encoding="utf-8"))
            self._schedules = state["schedules"]
            self._counter = state["counter"]
        except (OSError, ValueError, KeyError):
            self._schedules, self._counter = {}, 0

    def _save(self) -> None:
        payload = json.dumps({"version": 1, "schedules": self._schedules, "counter": self._counter})
        scratch = self._path.with_suffix(".tmp")
        scratch.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(payload, encoding="utf-8")
        os.replace(scratch, self._path)

    # -- wire ops ----------------------------------------------------------

    def handle(self, op: str, body: dict) -> dict:
        with self._lock:
            if self._session is None:
                raise ScheduleError("host_starting", "the local host is still starting", status=503)
            if op == "create":
                return {"schedule": self._create(body)}
            name = body.get("name")
            if op == "list":
                return {"schedules": [self._view(s) for s in self._schedules.values()]}
            if not isinstance(name, str) or name not in self._schedules:
                raise ScheduleError("not_found", f"no schedule named {name!r}", status=404)
            schedule = self._schedules[name]
            if op == "delete":
                del self._schedules[name]
                self._save()
                return {"deleted": self._view(schedule)}
            if op == "pause":
                schedule["state"] = "paused"
                self._touch(schedule)
                return {"paused": self._view(schedule)}
            if op == "resume":
                self._resume(schedule)
                return {"resumed": self._view(schedule)}
            if op == "runs":
                limit = body.get("limit", 20)
                limit = limit if isinstance(limit, int) and limit > 0 else 20
                return {"runs": list(reversed(schedule["runs"]))[:limit]}
            raise ScheduleError("not_found", f"unknown schedules operation {op!r}", status=404)

    def _create(self, body: dict) -> dict:
        name = body.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise ScheduleError("invalid_request", "name must be kebab-case (max 64 chars)")
        cadences = {k: body[k] for k in ("cron", "every", "at") if body.get(k) is not None}
        if len(cadences) != 1:
            raise ScheduleError("invalid_request", "exactly one of cron, every, at is required")
        tz_name = body.get("timezone") or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            raise ScheduleError("invalid_request", f"unknown timezone {tz_name!r}") from None
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise ScheduleError("invalid_request", "args must be an object")
        if len(json.dumps(args).encode("utf-8")) > MAX_ARGS_BYTES:
            raise ScheduleError("invalid_request", f"args exceed {MAX_ARGS_BYTES} bytes")
        resource_type, action = body.get("resource_type"), body.get("action")
        if not (isinstance(resource_type, str) and isinstance(action, str)):
            raise ScheduleError("invalid_request", "resource_type and action are required")
        _validate_target(self._session, resource_type, action)

        now = self._now()
        if "cron" in cadences:
            next_fire = Cron(cadences["cron"]).next_after(now, tz)
        elif "every" in cadences:
            parse_every(cadences["every"])
            next_fire = now  # first fire immediately, then every interval
        else:
            next_fire = parse_at(cadences["at"])
            if next_fire <= now:
                raise ScheduleError("invalid_request", "at must be a future instant")
        previous = self._schedules.get(name)
        schedule = {
            "name": name,
            "resource_type": resource_type,
            "action": action,
            "args": args,
            **cadences,
            "timezone": tz_name,
            "state": "active",
            "consecutive_failures": 0,
            "next_fire": next_fire.isoformat(),
            "created_at": previous["created_at"] if previous else now.isoformat(),
            "updated_at": now.isoformat(),
            "runs": previous["runs"] if previous else [],
        }
        self._schedules[name] = schedule
        self._save()
        return self._view(schedule)

    def _resume(self, schedule: dict) -> None:
        if schedule["state"] == "completed":
            raise ScheduleError("invalid_request", "a completed one-shot schedule cannot resume")
        schedule["state"] = "active"
        schedule["consecutive_failures"] = 0
        now = self._now()
        if schedule.get("cron"):
            tz = ZoneInfo(schedule["timezone"])
            schedule["next_fire"] = Cron(schedule["cron"]).next_after(now, tz).isoformat()
        elif schedule.get("every"):
            schedule["next_fire"] = now.isoformat()
        self._touch(schedule)

    def _touch(self, schedule: dict) -> None:
        schedule["updated_at"] = self._now().isoformat()
        self._save()

    @staticmethod
    def _view(schedule: dict) -> dict:
        return {k: v for k, v in schedule.items() if k != "runs"}

    # -- firing ------------------------------------------------------------

    def tick(self, now: datetime) -> None:
        with self._lock:
            session = self._session
            due = [
                dict(s)
                for s in self._schedules.values()
                if s["state"] == "active" and datetime.fromisoformat(s["next_fire"]) <= now
            ]
        if session is None:
            return
        for snapshot in due:
            self._fire(session, snapshot, now)

    def _fire(self, session, snapshot: dict, now: datetime) -> None:
        with self._lock:
            self._counter += 1
            run_id = f"schedule-run-{self._counter}"
        invoker = self._invoker
        if invoker is None:
            from .local_actions import invoke_backend_action

            invoker = invoke_backend_action
        error = None
        started = self._now()
        try:  # the lock is NOT held across the action call
            invoker(session, snapshot["resource_type"], snapshot["action"], snapshot["args"])
        except Exception as caught:
            error = str(caught)[:2000]
        with self._lock:
            schedule = self._schedules.get(snapshot["name"])
            if schedule is None:  # deleted while firing
                return
            run = {
                "id": run_id,
                "status": "failed" if error else "succeeded",
                "scheduled_for": schedule["next_fire"],
                "started": started.isoformat(),
                "finished": self._now().isoformat(),
            }
            if error:
                run["error"] = error
                schedule["consecutive_failures"] += 1
                if schedule["consecutive_failures"] >= AUTOPAUSE_AFTER:
                    schedule["state"] = "autopaused"
            else:
                schedule["consecutive_failures"] = 0
            schedule["runs"] = (schedule["runs"] + [run])[-RUNS_KEPT:]
            if schedule.get("at"):
                schedule["state"] = "completed" if schedule["state"] == "active" else schedule["state"]
            elif schedule.get("every"):
                # Missed windows (host down) collapse into the one fire above.
                schedule["next_fire"] = (now + timedelta(seconds=parse_every(schedule["every"]))).isoformat()
            else:
                tz = ZoneInfo(schedule["timezone"])
                schedule["next_fire"] = Cron(schedule["cron"]).next_after(now, tz).isoformat()
            schedule["updated_at"] = self._now().isoformat()
            self._save()
