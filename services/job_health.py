"""Реестр здоровья фоновых джоб и компонентов процесса для /health.

`tracked(name, callback, interval)` оборачивает колбэк job_queue: считает запуски,
время, падения подряд и последнюю ошибку, а исключение пробрасывает дальше —
error handler приложения видит его как и раньше. После
`JOB_ALERT_AFTER_FAILURES` падений подряд глобальные админы получают сообщение в
ЛС (не чаще раза в `JOB_ALERT_COOLDOWN_HOURS`), а когда джоба снова отработала —
одно сообщение о восстановлении.

Состояние живёт в памяти процесса: после рестарта /health показывает только то,
что произошло с момента старта (`STARTED_AT`).
"""
from __future__ import annotations

import functools
import html
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import config
from time_utils import now_msk

logger = logging.getLogger(__name__)

STARTED_AT: datetime = now_msk()

# Джоба считается зависшей, если не стартовала дольше STALE_FACTOR интервалов
# (плюс запас на `first=` при старте).
STALE_FACTOR = 3
STALE_GRACE_SECONDS = 300


@dataclass
class JobState:
    name: str
    interval: float
    runs: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_start: datetime | None = None
    last_ok: datetime | None = None
    last_error_at: datetime | None = None
    last_error: str | None = None
    last_duration_s: float | None = None
    running: bool = False
    alerted: bool = False
    last_alert_at: datetime | None = None


@dataclass
class ComponentState:
    name: str
    ok: bool
    detail: str = ""
    updated_at: datetime = field(default_factory=now_msk)


_jobs: dict[str, JobState] = {}
_components: dict[str, ComponentState] = {}


def reset() -> None:
    """Forget every job and component (tests)."""
    _jobs.clear()
    _components.clear()


def register(name: str, interval: float) -> JobState:
    state = _jobs.get(name)
    if state is None:
        state = JobState(name=name, interval=float(interval))
        _jobs[name] = state
    else:
        state.interval = float(interval)
    return state


def record_component(name: str, ok: bool, detail: str = "") -> None:
    _components[name] = ComponentState(name=name, ok=bool(ok), detail=str(detail or "")[:300])


def _short_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text if len(text) <= 300 else text[:297] + "…"


def _alert_due(state: JobState, now: datetime) -> bool:
    if state.consecutive_failures < config.JOB_ALERT_AFTER_FAILURES:
        return False
    if state.last_alert_at is None:
        return True
    return now - state.last_alert_at >= timedelta(hours=config.JOB_ALERT_COOLDOWN_HOURS)


async def _notify_admins(bot, text: str) -> int:
    sent = 0
    for admin_id in list(config.ADMIN_IDS):
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML")
            sent += 1
        except Exception as e:  # a blocked DM must not break the job wrapper
            logger.warning("Job alert to %s failed: %s", admin_id, e)
    return sent


def tracked(name: str, callback, interval: float):
    """Wrap a job_queue callback so /health can see it. Re-raises job errors."""
    state = register(name, interval)

    @functools.wraps(callback)
    async def wrapper(context, *args, **kwargs):
        started = time.monotonic()
        state.last_start = now_msk()
        state.runs += 1
        state.running = True
        try:
            result = await callback(context, *args, **kwargs)
        except Exception as exc:
            state.running = False
            state.last_duration_s = round(time.monotonic() - started, 3)
            state.failures += 1
            state.consecutive_failures += 1
            state.last_error_at = now_msk()
            state.last_error = _short_error(exc)
            now = state.last_error_at
            if _alert_due(state, now):
                state.last_alert_at = now
                state.alerted = True
                bot = getattr(context, "bot", None)
                if bot is not None:
                    await _notify_admins(bot, (
                        f"🚨 <b>Фоновая задача падает</b>: <code>{html.escape(name)}</code>\n"
                        f"Падений подряд: {state.consecutive_failures}\n"
                        f"Ошибка: <code>{html.escape(state.last_error)}</code>\n\n"
                        f"Подробности — /health"
                    ))
            raise
        state.running = False
        state.last_duration_s = round(time.monotonic() - started, 3)
        state.last_ok = now_msk()
        recovered = state.alerted
        failed_before = state.consecutive_failures
        state.consecutive_failures = 0
        state.alerted = False
        state.last_alert_at = None
        if recovered:
            bot = getattr(context, "bot", None)
            if bot is not None:
                await _notify_admins(bot, (
                    f"✅ <b>Фоновая задача восстановилась</b>: <code>{html.escape(name)}</code>\n"
                    f"До этого падала {failed_before} раз подряд."
                ))
        return result

    return wrapper


def is_stale(state: JobState, now: datetime | None = None) -> bool:
    now = now or now_msk()
    reference = state.last_start or STARTED_AT
    limit = timedelta(seconds=state.interval * STALE_FACTOR + STALE_GRACE_SECONDS)
    return now - reference > limit


def job_status(state: JobState, now: datetime | None = None) -> str:
    """'failing' | 'stale' | 'running' | 'ok' | 'waiting' (not started yet)."""
    if state.consecutive_failures:
        return "failing"
    if is_stale(state, now):
        return "stale"
    if state.running:
        return "running"
    if state.last_ok is None:
        return "waiting"
    return "ok"


def snapshot(now: datetime | None = None) -> dict:
    now = now or now_msk()
    jobs = []
    for state in sorted(_jobs.values(), key=lambda s: s.name):
        jobs.append({
            "name": state.name,
            "interval": state.interval,
            "status": job_status(state, now),
            "runs": state.runs,
            "failures": state.failures,
            "consecutive_failures": state.consecutive_failures,
            "last_start": state.last_start,
            "last_ok": state.last_ok,
            "last_error_at": state.last_error_at,
            "last_error": state.last_error,
            "last_duration_s": state.last_duration_s,
        })
    components = [
        {"name": c.name, "ok": c.ok, "detail": c.detail, "updated_at": c.updated_at}
        for c in sorted(_components.values(), key=lambda c: c.name)
    ]
    return {"started_at": STARTED_AT, "now": now, "jobs": jobs, "components": components}
