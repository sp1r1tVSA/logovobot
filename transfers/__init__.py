# Публичные точки подключения в остальном коде (по одной строке)
def apply_schema() -> None:
    from transfers.schema import apply_schema as _apply
    _apply()


def register_handlers(app) -> None:
    from transfers.handlers import register_handlers as _reg
    _reg(app)


def register_jobs(app) -> None:
    from transfers.jobs import job_auto_close
    app.job_queue.run_repeating(job_auto_close, interval=60, first=100, name="transfer_auto_close")


def register_routes(app) -> None:
    from transfers.api import register_routes as _reg
    _reg(app)


def set_bot(bot) -> None:
    from transfers.notify import set_bot as _set
    _set(bot)


__all__ = [
    "apply_schema",
    "register_handlers",
    "register_jobs",
    "register_routes",
    "set_bot",
]

