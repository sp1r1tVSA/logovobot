"""
tests/test_msk_time.py

Единые часы проекта: всё, что бот пишет, сравнивает и показывает, живёт по
Москве (UTC+3). Раньше в одной колонке встречались UTC (`CURRENT_TIMESTAMP`,
`datetime.now()` на UTC-сервере) и московское время (дедлайны туров, которые
админ вбивает руками), из-за чего дедлайн «20:00» истекал в 23:00 МСК.

Здесь проверяется и сам `time_utils`, и то, что исходники не откатились назад:
нет `CURRENT_TIMESTAMP`, нет голого `'now'` в SQL, и ни один INSERT не полагается
на UTC-дефолт колонки (на старых базах дефолт так и остался UTC — переписать его
нельзя, не пересобирая таблицу).
"""

import datetime as dt
import os
import re
import sqlite3
import unittest

import database
import time_utils
from time_utils import MSK, fmt_msk, now_msk, now_msk_str, parse_msk, today_msk

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SQL_NOW = "datetime('now', '+3 hours')"

# Каталоги с исполняемым кодом бота. `tests/` и агентская обвязка не сканируются.
SOURCE_DIRS = ("api", "handlers", "services", "scripts", "utils")
SOURCE_ROOT_FILES = ("database.py", "main.py", "config.py", "purge_old_season.py")


def _source_files() -> list[str]:
    files = [
        os.path.join(REPO_ROOT, name)
        for name in SOURCE_ROOT_FILES
        if os.path.exists(os.path.join(REPO_ROOT, name))
    ]
    for folder in SOURCE_DIRS:
        for root, dirs, names in os.walk(os.path.join(REPO_ROOT, folder)):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            files.extend(os.path.join(root, n) for n in names if n.endswith(".py"))
    return files


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# `schema_migrations.applied_at` — служебная отметка о самих миграциях, а не
# событие в жизни лиги: её никто не показывает и ни с чем не сравнивает.
UNTRACKED_TABLES = frozenset({"schema_migrations"})


def _default_timestamp_columns() -> dict[str, set[str]]:
    """{таблица: колонки} для колонок с временным дефолтом в текущей схеме."""
    database.init_db()
    columns: dict[str, set[str]] = {}
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        for (table,) in cursor.fetchall():
            if table in UNTRACKED_TABLES:
                continue
            cursor.execute(f"PRAGMA table_info({table})")
            for _cid, name, _type, _nn, default, _pk in cursor.fetchall():
                if default and "datetime(" in str(default):
                    columns.setdefault(table, set()).add(name)
    return columns


class TestTimeUtils(unittest.TestCase):
    def test_now_is_three_hours_ahead_of_utc(self):
        delta = now_msk() - dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual(delta.total_seconds(), 3 * 3600, delta=5)

    def test_now_is_naive(self):
        """Aware-значение сломало бы сравнение со строками из базы."""
        self.assertIsNone(now_msk().tzinfo)

    def test_now_str_matches_the_storage_format(self):
        self.assertRegex(now_msk_str(), r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_today_follows_moscow_midnight(self):
        self.assertEqual(today_msk(), now_msk().date())

    def test_naive_value_is_already_moscow(self):
        self.assertEqual(parse_msk("2026-09-19 08:42:11"), dt.datetime(2026, 9, 19, 8, 42, 11))

    def test_aware_value_is_converted(self):
        self.assertEqual(parse_msk("2026-09-19T08:42:11Z"), dt.datetime(2026, 9, 19, 11, 42, 11))
        self.assertEqual(
            parse_msk(dt.datetime(2026, 9, 19, 8, 42, 11, tzinfo=dt.timezone.utc)),
            dt.datetime(2026, 9, 19, 11, 42, 11),
        )

    def test_unparsable_values(self):
        self.assertIsNone(parse_msk("вчера"))
        self.assertIsNone(parse_msk(""))
        self.assertIsNone(parse_msk(None))

    def test_fmt_falls_back_without_raising(self):
        self.assertEqual(fmt_msk("2026-09-19 08:42:11", "%d.%m %H:%M"), "19.09 08:42")
        self.assertEqual(fmt_msk(None), "—")
        self.assertEqual(fmt_msk("вчера"), "вчера")

    def test_offset_is_fixed(self):
        """У Москвы нет перехода на летнее время с 2014 года."""
        self.assertEqual(MSK.utcoffset(None), dt.timedelta(hours=3))
        self.assertEqual(time_utils.SQL_NOW, SQL_NOW)


class TestDatabaseWritesMoscowTime(unittest.TestCase):
    def setUp(self) -> None:
        database.init_db()

    def test_inserted_row_carries_moscow_time(self):
        database.register_user(900_001, "msk_probe")
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT registered_at FROM users WHERE telegram_id = ?", (900_001,)
            ).fetchone()
        written = parse_msk(row["registered_at"])
        self.assertIsNotNone(written)
        self.assertLess(abs((written - now_msk()).total_seconds()), 120)

    def test_schema_defaults_are_moscow(self):
        for table, columns in _default_timestamp_columns().items():
            with self.subTest(table=table):
                with database.transaction() as conn:
                    defaults = {
                        name: str(dflt)
                        for _cid, name, _type, _nn, dflt, _pk in conn.cursor()
                        .execute(f"PRAGMA table_info({table})")
                        .fetchall()
                        if name in columns
                    }
                for column, default in defaults.items():
                    self.assertIn("+3 hours", default, f"{table}.{column}")


class TestUtcToMskMigration(unittest.TestCase):
    """018: старые строки на боевой базе сдвигаются один раз, человеческие — нет."""

    def setUp(self) -> None:
        database.init_db()

    def _raw(self) -> sqlite3.Connection:
        return sqlite3.connect(database.DB_PATH)

    def test_machine_timestamps_shift_and_handmade_ones_do_not(self):
        con = self._raw()
        con.execute(
            "INSERT INTO users (telegram_id, username, role, registered_at)"
            " VALUES (?, 'legacy', 'player', '2026-01-01 00:00:00')",
            (900_002,),
        )
        con.execute(
            "INSERT INTO rounds (division_id, round_number, deadline) VALUES (1, 91, '2026-01-01 20:00')"
        )
        con.execute(
            "INSERT INTO user_progression (user_id, last_active_date) VALUES (?, '2026-01-01')",
            (900_002,),
        )
        con.execute("DELETE FROM schema_migrations WHERE version = '018_utc_to_msk_timestamps'")
        con.commit()
        con.close()
        database.close_thread_connection()

        database.init_db()

        con = self._raw()
        self.assertEqual(
            con.execute("SELECT registered_at FROM users WHERE telegram_id = ?", (900_002,)).fetchone()[0],
            "2026-01-01 03:00:00",
        )
        self.assertEqual(
            con.execute("SELECT deadline FROM rounds WHERE round_number = 91").fetchone()[0],
            "2026-01-01 20:00",
        )
        self.assertEqual(
            con.execute(
                "SELECT last_active_date FROM user_progression WHERE user_id = ?", (900_002,)
            ).fetchone()[0],
            "2026-01-01",
        )
        con.close()
        database.close_thread_connection()

        # Второй прогон — уже no-op: маркер на месте.
        database.init_db()
        con = self._raw()
        self.assertEqual(
            con.execute("SELECT registered_at FROM users WHERE telegram_id = ?", (900_002,)).fetchone()[0],
            "2026-01-01 03:00:00",
        )
        con.close()

    def test_unparsable_value_is_left_alone(self):
        con = self._raw()
        con.execute("INSERT INTO seasons (name, status) VALUES ('broken', 'draft')")
        con.execute("UPDATE seasons SET created_at = 'не дата' WHERE name = 'broken'")
        con.execute("DELETE FROM schema_migrations WHERE version = '018_utc_to_msk_timestamps'")
        con.commit()
        con.close()
        database.close_thread_connection()

        database.init_db()

        con = self._raw()
        self.assertEqual(
            con.execute("SELECT created_at FROM seasons WHERE name = 'broken'").fetchone()[0],
            "не дата",
        )
        con.close()


class TestSourcesStayOnOneClock(unittest.TestCase):
    """Статические проверки: код не должен возвращаться к UTC."""

    def test_no_current_timestamp_in_sql(self):
        offenders = []
        for path in _source_files():
            for number, line in enumerate(_read(path).splitlines(), 1):
                code = line.split("#", 1)[0]
                if re.search(r"\bCURRENT_(TIMESTAMP|DATE|TIME)\b", code):
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"CURRENT_TIMESTAMP пишет UTC, нужен {SQL_NOW}: {offenders}")

    def test_no_bare_now_in_sql(self):
        """`datetime('now')` и родня — это UTC; в проекте всегда со сдвигом +3."""
        pattern = re.compile(r"(datetime|date|julianday|strftime)\(\s*('%[^']*',\s*)?'now'(?!\s*,\s*'\+3 hours')")
        offenders = []
        for path in _source_files():
            for number, line in enumerate(_read(path).splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"Голое 'now' в SQL отдаёт UTC: {offenders}")

    def test_no_naive_python_now(self):
        """`datetime.now()` без зоны — это часы сервера, в Docker UTC."""
        pattern = re.compile(r"datetime\.now\(\s*\)|datetime\.utcnow\(\s*\)|date\.today\(\s*\)")
        offenders = []
        for path in _source_files():
            for number, line in enumerate(_read(path).splitlines(), 1):
                if pattern.search(line) and "time_utils" not in line:
                    offenders.append(f"{os.path.relpath(path, REPO_ROOT)}:{number}")
        self.assertEqual(offenders, [], f"Нужен now_msk()/today_msk(): {offenders}")

    def test_inserts_do_not_rely_on_the_column_default(self):
        """На старых базах дефолт колонки так и остался UTC — время пишем явно.

        Переписать дефолт нельзя, не пересобрав таблицу, а это запрещено
        (только аддитивные миграции), поэтому единственный способ — перечислять
        колонку времени в каждом INSERT.
        """
        timestamp_columns = _default_timestamp_columns()
        insert_re = re.compile(
            r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+([A-Za-z_0-9]+)\s*\(([^)]*)\)", re.IGNORECASE | re.DOTALL
        )
        offenders = []
        for path in _source_files():
            text = _read(path)
            for match in insert_re.finditer(text):
                table = match.group(1)
                expected = timestamp_columns.get(table)
                if not expected:
                    continue
                listed = match.group(2)
                if "{" in listed:
                    continue  # список колонок собирается на лету из самой строки
                if not re.fullmatch(r"[\s\w,]+", listed):
                    continue  # не перечисление колонок, а проза: упоминание в докстринге
                line = text[: match.start()].count("\n") + 1
                for column in sorted(expected):
                    if not re.search(rf"\b{column}\b", listed):
                        offenders.append(
                            f"{os.path.relpath(path, REPO_ROOT)}:{line} ({table}.{column})"
                        )
        self.assertEqual(offenders, [], f"INSERT полагается на UTC-дефолт: {offenders}")


if __name__ == "__main__":
    unittest.main()
