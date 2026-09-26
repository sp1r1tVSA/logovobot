"""
Pytest bootstrap for the whole repository.

This file is imported by pytest *before* any test module, which is the only
moment where we can still redirect the SQLite path: `config.py` resolves
`DB_PATH` at import time and `database.py` does `from config import DB_PATH`.

Every pytest process gets its **own** database file inside a temporary
directory. The real `league.db` in the repo root is never touched by the test
suite any more, and — more importantly — `pytest -n auto` workers no longer
fight over a single WAL file, so the suite can run in parallel.
"""

import atexit
import os
import shutil
import tempfile

# xdist sets this in every worker process ("gw0", "gw1", ...); absent when the
# suite runs single-process.
_WORKER = os.environ.get("PYTEST_XDIST_WORKER", "main")

_TMP_DIR = tempfile.mkdtemp(prefix=f"logovobot-tests-{_WORKER}-")
atexit.register(shutil.rmtree, _TMP_DIR, ignore_errors=True)

# `load_dotenv()` in config.py does not override already-present variables,
# so setting this here wins over a local .env.
os.environ["LEAGUE_SQLITE_PATH"] = os.path.join(_TMP_DIR, "league.db")

# То же окно и та же причина: десяток тестовых файлов делают
# `from config import TOKEN` на уровне модуля, то есть снимают значение до
# запуска любой фикстуры. Без .env оно приходит None, а initData тесты
# подписывают плейсхолдером — подписи расходятся, и запросы получают 401.
# Значение — публичный пример из документации Telegram; setdefault оставляет
# приоритет за переменной, уже заданной в окружении, а настоящий токен из .env
# в тесты не попадает и попадать не должен.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")

# Та же история с админами: часть файлов берёт `ADMIN_IDS[0]` на уровне модуля
# и ждёт, что этот id пройдёт в /api/admin/*. С пустым списком они выбирают
# запасной id, который админом не является, и получают 403. Id заведомо вне
# диапазонов, которые тесты используют под обычных игроков.
os.environ.setdefault("ADMIN_IDS", "990000001")

# Срок приёма долгосрочных ставок — реальная дата, и после неё тесты outright
# стали бы падать сами собой. Здесь срока нет; тесты срока подменяют
# `config.OUTRIGHT_BETS_CLOSE_AT` сами.
os.environ["OUTRIGHT_BETS_CLOSE_AT"] = ""

import pytest  # noqa: E402  (must come after the env var above)


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """
    Build the schema once per pytest process.

    Most test files call `init_db()` themselves, but a few (e.g.
    test_production_audit.py) used to rely on the repo-root league.db already
    existing. With a fresh temp database they need the tables created up front.
    """
    import database

    database.init_db()


@pytest.fixture(scope="module", autouse=True)
def _isolate_db_path(tmp_path_factory):
    """
    Give every test module its own database file.

    Sharing one file per worker made files order-dependent in two ways: a dozen
    of them point the global DB_PATH at their own temp file and never put it
    back (test_phase9_*.py, test_phase5_advanced_betting.py,
    test_phase2_architecture.py, ...), and files that clean up by id range
    (`DELETE FROM users WHERE telegram_id >= 778000`) tripped over child rows
    another file had left behind, failing with FOREIGN KEY constraint failed.
    A fresh file per module removes both by construction; `init_db()` on an
    empty file costs ~40 ms, which parallel workers absorb.
    """
    import config
    import database

    orig_db, orig_cfg = database.DB_PATH, config.DB_PATH

    database.close_thread_connection()
    database.DB_PATH = config.DB_PATH = str(tmp_path_factory.mktemp("db") / "league.db")
    database.init_db()

    yield

    database.close_thread_connection()
    database.DB_PATH, config.DB_PATH = orig_db, orig_cfg


@pytest.fixture(autouse=True)
def _disable_api_rate_limit():
    """
    Выключить rate limiting API на время обычных тестов.

    В проде лимиты включены по умолчанию, но тестовые сценарии бьют по одним и
    тем же эндпоинтам подряд от одного user_id и мгновенно упирались бы в окно
    и в минимальный интервал между ставками. Файлы, которые проверяют сами
    лимиты, включают флаг обратно у себя.
    """
    import config
    from api import rate_limiter

    original = config.API_RATE_LIMIT_ENABLED
    config.API_RATE_LIMIT_ENABLED = False
    rate_limiter.reset_all()
    yield
    config.API_RATE_LIMIT_ENABLED = original
    rate_limiter.reset_all()


_CANONICAL_ADMIN_IDS = None


@pytest.fixture(autouse=True)
def _reset_line_refresh():
    """
    Сбросить троттлинг переоценки линии (`services/line_refresh`) между тестами.

    Окно в минуту на тур хранится в памяти процесса: без сброса тест, который
    меняет матчи тура и снова запрашивает /api/markets/tours, получал бы линию,
    не пересчитанную после предыдущего теста.
    """
    from services import line_refresh

    line_refresh.reset()
    yield
    line_refresh.reset()


@pytest.fixture(autouse=True)
def _stable_admin_ids():
    """
    Держать `config.ADMIN_IDS` одним и тем же объектом списка на весь процесс.

    Часть файлов снимает список на импорте (`from config import ADMIN_IDS`) и
    потом дописывает туда своего админа через `.append()`, рассчитывая, что
    правку увидит и `config`. Другая часть подменяет сам атрибут новым списком,
    иногда не возвращая старый. С `--dist loadfile` оба вида файлов попадают в
    один воркер, и первый вид начинает править список, на который `config` уже
    не смотрит: админ перестаёт быть админом, /api/admin/* отдаёт 403.

    Фикстура возвращает исходный объект на место после каждого теста и
    восстанавливает его содержимое — подмены внутри теста при этом работают
    как раньше.
    """
    import config

    global _CANONICAL_ADMIN_IDS
    if _CANONICAL_ADMIN_IDS is None:
        _CANONICAL_ADMIN_IDS = config.ADMIN_IDS

    snapshot = list(config.ADMIN_IDS)
    yield
    _CANONICAL_ADMIN_IDS[:] = snapshot
    config.ADMIN_IDS = _CANONICAL_ADMIN_IDS


@pytest.fixture(autouse=True)
def _fresh_connection():
    """
    Drop the cached per-thread SQLite connection around every test.

    `transaction()` keeps one connection open per thread for speed. Several
    test files isolate themselves by deleting their .db file in setUp/tearDown,
    and on Windows that silently fails while a connection still holds the file
    open — leaving stale rows behind. Closing here costs well under a
    millisecond per test and keeps that pattern working.
    """
    import database

    database.close_thread_connection()
    yield
    database.close_thread_connection()
