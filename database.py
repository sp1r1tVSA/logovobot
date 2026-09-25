import logging
import sqlite3
import datetime
import re
import threading
import asyncio
import json
from typing import Generator
from contextlib import contextmanager
from config import DB_PATH, INITIAL_WALLET_BALANCE, MAX_OPEN_ROUNDS_PER_DIVISION, ROUND_DEADLINE_REMINDER_HOURS
from constants import CUP_DIVISION_SENTINEL, CUP_SERIES_GAMES, CUP_STAGES, CUP_STAGE_ORDER
from time_utils import SQL_NOW, now_msk, now_msk_str, today_msk
from club_registry import normalize_team_name, resolve_team_name
from services import debt_policy
from services.player_names import (
    normalize_player_name_key,
    normalize_footballer_name,
    is_same_footballer,
    match_roster_name,
)

logger = logging.getLogger(__name__)

# 322-защита: канонический код и текст отказа при ставке на свой собственный матч.
# Используются и place_user_bet, и services/risk_engine.RiskEngine, чтобы Telegram,
# Mini App и REST API отвечали одинаково.
SELF_BET_ERROR_CODE = "SELF_BET_PROHIBITED"
SELF_BET_ERROR_MESSAGE = "Запрещено делать ставки на матчи с собственным участием."


class RoundScheduleMissingError(ValueError):
    """Тур нельзя открыть: для (season, division, round) нет ни одного матча.

    Открытый тур без расписания — «фантом»: игроки видят приглашение вносить
    результаты, а вносить нечего; дедлайн и долговой трекер при этом уже идут.
    Наследуется от ValueError, потому что вызывающие уже ловят ValueError от
    сезонного гейта в тех же самых местах.
    """

    def __init__(self, round_number: int, division_id: int | None, season_id: int | None = None):
        self.round_number = round_number
        self.division_id = division_id
        self.season_id = season_id
        super().__init__(
            f"Round {round_number} (division={division_id}, season={season_id}) has no matches: "
            "the schedule has not been generated yet."
        )


class MaxActiveRoundsExceededError(ValueError):
    """Бросается, когда открытие тура превышает лимит туров с неистекшим дедлайном.

    Активным считается тур с `is_open = 1` И `deadline > now()`. Число сыгранных
    матчей не проверяется: слот освобождает только наступление дедлайна, зато
    освобождает сам — админу не нужно закрывать старые туры руками.

    Наследуется от ValueError по той же причине, что и
    `RoundScheduleMissingError`: вызывающие уже ловят ValueError от сезонного
    гейта в тех же самых местах.
    """

    def __init__(self, division_id: int, active_rounds: list[int], limit: int = MAX_OPEN_ROUNDS_PER_DIVISION):
        self.division_id = division_id
        self.active_rounds = active_rounds
        self.limit = limit
        super().__init__(
            f"В дивизионе #{division_id} уже открыто {len(active_rounds)} тура(ов) "
            f"с действующим дедлайном ({active_rounds}). Лимит: {limit}."
        )


class RoundDeadlineError(ValueError):
    """Дедлайн тура не задан, не разбирается или уже прошёл.

    Тур без дедлайна долгов не порождает и в трекер не попадает, поэтому
    открыть его так больше нельзя: админ обязан назвать срок.
    """

    def __init__(self, deadline: str | None, reason: str):
        self.deadline = deadline
        self.reason = reason
        super().__init__(reason)


def get_connection() -> sqlite3.Connection:
    """Establish and return a new SQLite database connection."""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn
    except sqlite3.Error as e:
        logger.exception(f"Failed to connect to database at {DB_PATH}")
        raise


_tx_local = threading.local()


def _thread_connection() -> sqlite3.Connection:
    """
    Return this thread's cached connection, opening one on first use.

    Opening a connection is expensive: `PRAGMA journal_mode=WAL` alone costs
    several milliseconds because it has to touch the WAL file and its locks.
    Since transaction() used to open and close a connection on every call,
    that overhead dominated. Caching one connection per thread keeps each
    transaction at roughly a millisecond.

    The cache is keyed by DB_PATH so that swapping `database.DB_PATH` (which
    several tests do) transparently drops the stale connection.
    """
    conn = getattr(_tx_local, "conn", None)
    if conn is not None and getattr(_tx_local, "conn_path", None) == DB_PATH:
        return conn

    close_thread_connection()
    conn = get_connection()
    _tx_local.conn = conn
    _tx_local.conn_path = DB_PATH
    return conn


def close_thread_connection() -> None:
    """Drop this thread's cached connection (on error or DB_PATH change)."""
    conn = getattr(_tx_local, "conn", None)
    _tx_local.conn = None
    _tx_local.conn_path = None
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass


@contextmanager
def transaction() -> Generator[sqlite3.Connection, None, None]:
    """Provide a transactional scope around database operations.

    Re-entrant: a nested transaction() call on the same thread joins the
    outer transaction (returns the same connection) instead of opening an
    independent one. Commit happens only when the OUTERMOST scope exits;
    a rollback rolls back the entire multi-step operation. This makes
    composite actions (e.g. confirm match + update cup series) atomic.

    The underlying connection is cached per thread and stays open between
    scopes; only the transaction boundary (commit/rollback) is per-scope.
    """
    stack = getattr(_tx_local, "stack", None)
    if stack:
        # Nested scope: reuse the outer connection, never commit here.
        yield stack[-1]
        return

    conn = _thread_connection()
    _tx_local.stack = [conn]
    try:
        yield conn
        conn.commit()
    except Exception as e:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        logger.exception("Transaction rolled back due to error")
        # Never hand a connection of unknown state to the next caller.
        close_thread_connection()
        raise
    finally:
        _tx_local.stack = []


# ─── 018: одна стрелка часов ─────────────────────────────────────────────────
# Эти колонки пишет сам бот. Раньше туда попадал UTC (`CURRENT_TIMESTAMP` и
# `datetime.now()` на UTC-сервере), теперь — Москва (см. time_utils). Разовый
# сдвиг старых строк, чтобы в одной колонке не жили два часовых пояса.
#
# Сознательно НЕ сдвигаются:
#   rounds.deadline, matches.match_time / match_date / proposed_time — их руками
#     вбивает админ, и они и раньше были московскими;
#   user_progression.last_active_date — только дата, без времени;
#   schema_migrations.applied_at — служебная отметка о самих миграциях.
#
# Имена таблиц и колонок подставляются в SQL текстом. Список захардкожен здесь и
# никогда не приходит снаружи — тот же случай, что SAFE_COLUMNS.
_MSK_SHIFT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("admin_audit_log", "created_at"),
    ("bet_audit_log", "created_at"),
    ("bet_markets", "created_at"),
    ("chat_history", "created_at"),
    ("coin_transactions", "created_at"),
    ("debt_reminders", "sent_at"),
    ("division_admins", "created_at"),
    ("division_topics", "created_at"),
    ("divisions", "created_at"),
    ("elo_applied_matches", "applied_at"),
    ("favorites", "created_at"),
    ("integrity_cases", "created_at"),
    ("integrity_cases", "updated_at"),
    ("integrity_cases", "reviewed_at"),
    ("live_events", "created_at"),
    ("live_match_states", "last_updated_at"),
    ("live_statistics", "updated_at"),
    ("market_selections", "updated_at"),
    ("markets", "created_at"),
    ("matches", "played_at"),
    ("matches", "frozen_at"),
    ("notification_events", "created_at"),
    ("notification_events", "sent_at"),
    ("notifications", "created_at"),
    ("odds_history", "changed_at"),
    ("odds_movement", "created_at"),
    ("pending_reports", "created_at"),
    ("prediction_snapshots", "snapshot_at"),
    ("predictions", "created_at"),
    ("predictions", "resolved_at"),
    ("provider_matches", "last_update_at"),
    ("provider_sync_log", "created_at"),
    ("provider_sync_state", "updated_at"),
    ("provider_sync_state", "last_sync_at"),
    ("risk_alerts", "created_at"),
    ("risk_alerts", "resolved_at"),
    ("risk_limits_config", "updated_at"),
    ("round_content_posts", "posted_at"),
    ("round_reminders", "sent_at"),
    ("rounds", "bets_opened_at"),
    ("saved_coupons", "created_at"),
    ("season_player_stats", "updated_at"),
    ("season_reward_ledger", "created_at"),
    ("season_reward_ledger", "distributed_at"),
    ("season_rules_config", "created_at"),
    ("season_snapshots", "created_at"),
    ("seasons", "created_at"),
    ("seasons", "started_at"),
    ("seasons", "finished_at"),
    ("sports_providers", "created_at"),
    ("sports_providers", "updated_at"),
    ("sports_providers", "last_sync_at"),
    ("style_samples", "created_at"),
    ("team_ratings", "last_updated_at"),
    ("teams", "created_at"),
    ("telegram_media_cache", "created_at"),
    ("tournaments", "created_at"),
    ("user_achievements", "unlocked_at"),
    ("user_bets", "created_at"),
    ("user_bets", "settled_at"),
    ("user_bets", "cashout_at"),
    ("user_progression", "updated_at"),
    ("user_wallets", "updated_at"),
    ("user_wallets", "last_bonus_at"),
    ("user_warns", "created_at"),
    ("users", "registered_at"),
)


def _shift_timestamps_to_msk(cursor: sqlite3.Cursor) -> int:
    """Сдвинуть машинные отметки времени из UTC в МСК. Возвращает число строк."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables = {row[0] for row in cursor.fetchall()}
    shifted = 0
    for table, column in _MSK_SHIFT_COLUMNS:
        if table not in tables:
            continue
        cursor.execute(f"PRAGMA table_info({table})")
        if column not in {row[1] for row in cursor.fetchall()}:
            continue
        # datetime() отдаёт NULL на неразбираемой строке — такие строки не трогаем,
        # иначе миграция стёрла бы то, что не смогла прочитать.
        cursor.execute(
            f"UPDATE {table} SET {column} = datetime({column}, '+3 hours') "
            f"WHERE {column} IS NOT NULL AND datetime({column}) IS NOT NULL"
        )
        shifted += cursor.rowcount
def _deduplicate_squad_players_in_db(cursor: sqlite3.Cursor) -> int:
    """Find and merge any duplicate records in squad_players for the same club by norm_team_name and norm_name."""
    cursor.execute("""
        SELECT norm_team_name, norm_name, COUNT(*) AS cnt
        FROM squad_players
        WHERE norm_name IS NOT NULL AND norm_name != '' AND norm_team_name IS NOT NULL AND norm_team_name != ''
        GROUP BY norm_team_name, norm_name
        HAVING cnt > 1
    """)
    dup_groups = cursor.fetchall()
    merged_count = 0
    for group in dup_groups:
        t_norm = group["norm_team_name"]
        n_key = group["norm_name"]
        cursor.execute("""
            SELECT id, team_name, player_name, position, norm_team_name
            FROM squad_players
            WHERE norm_team_name = ? AND norm_name = ?
            ORDER BY id ASC
        """, (t_norm, n_key))
        records = cursor.fetchall()
        if len(records) <= 1:
            continue
        canonical = records[0]
        canon_id = canonical["id"]
        canon_name = canonical["player_name"]
        canon_pos = canonical["position"]

        for duplicate in records[1:]:
            dup_id = duplicate["id"]
            dup_name = duplicate["player_name"]
            dup_pos = duplicate["position"]

            # If canonical has no position but duplicate has one, preserve it
            if not canon_pos and dup_pos:
                canon_pos = dup_pos
                cursor.execute("UPDATE squad_players SET position = ? WHERE id = ?", (dup_pos, canon_id))

            # Re-point any match_events referencing duplicate name
            cursor.execute("""
                UPDATE match_events
                SET player_name = ?
                WHERE (LOWER(team_name) = ? OR LOWER(team_name) = ?) AND player_name = ?
            """, (canon_name, canonical["team_name"].lower(), duplicate["team_name"].lower(), dup_name))

            # Re-point any matches.mvp_player referencing duplicate name
            cursor.execute("""
                UPDATE matches
                SET mvp_player = ?
                WHERE mvp_player = ? AND (
                    LOWER(player1_team) IN (?, ?) OR LOWER(player2_team) IN (?, ?)
                )
            """, (canon_name, dup_name,
                  canonical["team_name"].lower(), duplicate["team_name"].lower(),
                  canonical["team_name"].lower(), duplicate["team_name"].lower()))

            # Delete the duplicate row
            cursor.execute("DELETE FROM squad_players WHERE id = ?", (dup_id,))
            merged_count += 1
            logger.info(
                "Merged duplicate squad player: id=%d ('%s') into canonical id=%d ('%s') in '%s'",
                dup_id, dup_name, canon_id, canon_name, canonical["team_name"]
            )
    return merged_count


PREDICTION_UNIQUE_INDEX = "uniq_predictions_match_model"
MIGRATION_021_PREDICTION_UNIQUE = "021_prediction_one_row_per_model"
MIGRATION_019_PREDICTION_UNIQUE = MIGRATION_021_PREDICTION_UNIQUE


def _ensure_prediction_uniqueness(cursor: sqlite3.Cursor) -> bool:
    """Поднять predictions к правилу «(match_id, model_version) = одна строка».

    Возвращает True только если уникальный индекс действительно стоит.

    Идемпотентность записи обеспечивает ``save_ai_prediction`` (она работает и без
    индекса), этот индекс закрепляет то же самое на уровне схемы, чтобы дубли не
    могли вернуться ни одним из путей записи.

    Миграция аддитивная и ничего не удаляет. Исторический
    ``idx_predictions_match`` не уникальный, поэтому в уже развёрнутой базе дубли
    есть — а на них ``CREATE UNIQUE INDEX`` падает с «index ... is not unique» и
    оборвал бы весь ``init_db()``. Вместо молчаливой очистки: считаем дубли,
    докладываем о них в logger.error точными числами и выходим, не записывая строку
    в ``schema_migrations``. Следующий старт повторит попытку, а развёрнутая база
    при этом остаётся нетронутой — удаление исторических прогнозов это отдельная
    задача, не эта миграция.
    """
    cursor.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_019_PREDICTION_UNIQUE,))
    if cursor.fetchone():
        return True

    cursor.execute("""
        SELECT COUNT(*) AS dup_keys, COALESCE(SUM(extra), 0) AS redundant_rows
        FROM (
            SELECT COUNT(*) - 1 AS extra
            FROM predictions
            GROUP BY match_id, model_version
            HAVING COUNT(*) > 1
        )
    """)
    stats = cursor.fetchone()
    dup_keys = stats["dup_keys"] if stats else 0
    redundant_rows = stats["redundant_rows"] if stats else 0
    if dup_keys:
        logger.error(
            "Migration %s not applied: predictions holds %s duplicated (match_id, model_version) "
            "key(s) across %s redundant row(s). A UNIQUE index cannot be created over them and "
            "nothing was deleted. Existing rows keep their ids; save_ai_prediction stays "
            "idempotent through its single-statement guard until the duplicates are resolved.",
            MIGRATION_019_PREDICTION_UNIQUE, dup_keys, redundant_rows
        )
        return False

    cursor.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {PREDICTION_UNIQUE_INDEX} "
        f"ON predictions(match_id, model_version)"
    )
    cursor.execute("""
        INSERT OR IGNORE INTO schema_migrations (version, description)
        VALUES (?, 'predictions: one row per (match_id, model_version)')
    """, (MIGRATION_019_PREDICTION_UNIQUE,))
    logger.info("Migration %s: unique index on predictions(match_id, model_version) created",
                MIGRATION_019_PREDICTION_UNIQUE)
    return True


MIGRATION_022_CUP_GENERAL = "022_cup_general_stage"
MIGRATION_023_CUP_TOPICS = "023_cup_topics"
CUP_SERIES_UNIQUE_INDEX = "idx_cup_series_stage_num_unique"


def _ensure_cup_schema(cursor: sqlite3.Cursor) -> bool:
    """Довести `cup_series` до схемы общего кубка (миграция 022).

    Таблица заведена давно, но ни один продуктовый путь в неё ничего не писал, так
    что дублей в развёрнутой базе быть не должно. Проверка ниже всё равно стоит
    дешёвой ценой: индекс создаётся один раз, а промахнуться на ней — значит уронить
    `init_db()` на пустой по факту, но не проверенной таблице.

    Уникальный индекс на (stage, series_num) — не косметика: без него повторная
    жеребьёвка того же этапа молча удваивает сетку, и `get_cup_bracket` начинает
    показывать две серии с одним номером.

    Правило то же, что у `predictions`: миграция аддитивная, ничего не удаляет, а
    при найденных дублях не применяется и остаётся в logger.error — поверх дублей
    ``CREATE UNIQUE INDEX`` упал бы и оборвал весь ``init_db()``.
    """
    cursor.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_022_CUP_GENERAL,))
    if cursor.fetchone():
        return True

    try:
        cursor.execute("ALTER TABLE cup_series ADD COLUMN stage_id INTEGER")
    except sqlite3.OperationalError:
        pass  # колонка уже добавлена предыдущим стартом
    try:
        cursor.execute("ALTER TABLE cup_series ADD COLUMN winner_source TEXT")
    except sqlite3.OperationalError:
        pass

    cursor.execute("""
        SELECT COUNT(*) AS dup_keys, COALESCE(SUM(extra), 0) AS redundant_rows
        FROM (
            SELECT COUNT(*) - 1 AS extra
            FROM cup_series
            GROUP BY stage, series_num
            HAVING COUNT(*) > 1
        )
    """)
    stats = cursor.fetchone()
    dup_keys = stats["dup_keys"] if stats else 0
    redundant_rows = stats["redundant_rows"] if stats else 0
    if dup_keys:
        logger.error(
            "Migration %s not applied: cup_series holds %s duplicated (stage, series_num) "
            "key(s) across %s redundant row(s). A UNIQUE index cannot be created over them "
            "and nothing was deleted.",
            MIGRATION_022_CUP_GENERAL, dup_keys, redundant_rows
        )
        return False

    cursor.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {CUP_SERIES_UNIQUE_INDEX} "
        f"ON cup_series(stage, series_num)"
    )
    cursor.execute("""
        INSERT OR IGNORE INTO schema_migrations (version, description)
        VALUES (?, 'Общий кубок: cup_stages, matches.stage_id/cup_winner_team, cup_series.stage_id/winner_source')
    """, (MIGRATION_022_CUP_GENERAL,))
    logger.info("Migration %s: unique index on cup_series(stage, series_num) created",
                MIGRATION_022_CUP_GENERAL)
    return True


MIGRATION_027_DIVISION_CUPS = "027_division_cups"
CUP_SERIES_STAGE_ID_UNIQUE_INDEX = "idx_cup_series_stage_id_num_unique"


def _ensure_division_cups(cursor: sqlite3.Cursor) -> bool:
    """Миграция 027: кубки дивизионов рядом с общим.

    `cup_stages` несёт встроенный UNIQUE(season_id, stage), снять который без
    пересборки таблицы нельзя, а пересобирать таблицы здесь запрещено. Поэтому
    этап кубка дивизиона хранится под ключом `cup_stage_key` («1/8@D3»), а
    читатели получают из `_cup_stage_dict` обычное имя стадии и `division_id` —
    новая колонка, NULL у общего кубка.

    Ключ уникальности серий переезжает с (stage, series_num) на (stage_id,
    series_num): у пяти кубков дивизионов в одном сезоне одинаковые стадии, и
    старый индекс не дал бы завести вторую 1/8. Это индекс, а не таблица, —
    удалить его можно. При найденных дублях миграция не применяется, как 022.

    `cup_series.announced_winner` — кому бот уже объявил проход в теме «Кубок»:
    повторное подтверждение игры не повторяет объявление, а смена победителя
    после сброса игры объявляется заново.
    """
    for ddl in (
        "ALTER TABLE cup_stages ADD COLUMN division_id INTEGER",
        "ALTER TABLE cup_series ADD COLUMN announced_winner TEXT",
    ):
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # колонка уже добавлена предыдущим стартом

    cursor.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_027_DIVISION_CUPS,))
    if not cursor.fetchone():
        cursor.execute("""
            SELECT COUNT(*) AS dup_keys
            FROM (
                SELECT 1 FROM cup_series
                WHERE stage_id IS NOT NULL
                GROUP BY stage_id, series_num
                HAVING COUNT(*) > 1
            )
        """)
        row = cursor.fetchone()
        if row and row["dup_keys"]:
            logger.error(
                "Migration %s not applied: cup_series holds %s duplicated (stage_id, series_num) "
                "key(s). Nothing was deleted.",
                MIGRATION_027_DIVISION_CUPS, row["dup_keys"]
            )
            return False
        cursor.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {CUP_SERIES_STAGE_ID_UNIQUE_INDEX} "
            f"ON cup_series(stage_id, series_num)"
        )
        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES (?, 'Кубки дивизионов: cup_stages.division_id, серии уникальны в пределах этапа')
        """, (MIGRATION_027_DIVISION_CUPS,))
        logger.info("Migration %s: division cups enabled", MIGRATION_027_DIVISION_CUPS)

    # Старый индекс снимается на каждом старте, а не один раз: 022, не
    # применённая из-за дублей, создаёт его заново, как только дубли уберут.
    cursor.execute(f"DROP INDEX IF EXISTS {CUP_SERIES_UNIQUE_INDEX}")
    return True


def init_db() -> None:
    """Initialize the database tables."""
    logger.info("Initializing database tables...")
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                description TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft', 'active', 'finished', 'archived')),
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                started_at TIMESTAMP,
                finished_at TIMESTAMP,
                created_by INTEGER
            )
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO seasons (id, name, status, created_at, started_at)
            VALUES (1, 'Сезон 2026', 'active', datetime('now', '+3 hours'), datetime('now', '+3 hours'))
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT,
                team_name TEXT,
                league_name TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                registered_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER,
                round_number INTEGER,
                player1_id INTEGER,
                player2_id INTEGER,
                player1_score INTEGER,
                player2_score INTEGER,
                status TEXT NOT NULL DEFAULT 'pending',
                played_at TIMESTAMP,
                FOREIGN KEY(player1_id) REFERENCES users(telegram_id),
                FOREIGN KEY(player2_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS squad_players (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                player_name TEXT NOT NULL,
                position TEXT,
                norm_name TEXT,
                norm_team_name TEXT,
                UNIQUE(team_name, player_name)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS match_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                team_name TEXT NOT NULL,
                player_name TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN ('goal', 'assist')),
                count INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL DEFAULT 1,
                division_id INTEGER NOT NULL DEFAULT 1,
                round_number INTEGER NOT NULL,
                is_open BOOLEAN DEFAULT 0,
                deadline TEXT,
                UNIQUE(season_id, division_id, round_number)
            )
        """)

        # Migration: check if rounds still uses legacy schema or lacks season_id
        try:
            cursor.execute("PRAGMA table_info(rounds)")
            r_cols = cursor.fetchall()
            r_col_names = [c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in r_cols]
            r_pk_cols = [c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in r_cols if (c["pk"] if isinstance(c, sqlite3.Row) else c[5]) > 0]
            div_col = next((c for c in r_cols if (c["name"] if isinstance(c, sqlite3.Row) else c[1]) == "division_id"), None)
            div_dflt = (div_col["dflt_value"] if isinstance(div_col, sqlite3.Row) else div_col[4]) if div_col else None
            has_season = "season_id" in r_col_names

            if "id" not in r_col_names or r_pk_cols == ["round_number"] or div_dflt is None or not has_season:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS rounds_v3 (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        season_id INTEGER NOT NULL DEFAULT 1,
                        division_id INTEGER NOT NULL DEFAULT 1,
                        round_number INTEGER NOT NULL,
                        is_open BOOLEAN DEFAULT 0,
                        deadline TEXT,
                        UNIQUE(season_id, division_id, round_number)
                    )
                """)
                if "id" in r_col_names and "division_id" in r_col_names:
                    cursor.execute("""
                        INSERT OR IGNORE INTO rounds_v3 (id, season_id, division_id, round_number, is_open, deadline)
                        SELECT id, 1, COALESCE(division_id, 1), round_number, is_open, deadline
                        FROM rounds
                    """)
                elif "division_id" in r_col_names:
                    cursor.execute("""
                        INSERT OR IGNORE INTO rounds_v3 (season_id, division_id, round_number, is_open, deadline)
                        SELECT 1, COALESCE(division_id, 1), round_number, is_open, deadline
                        FROM rounds
                    """)
                else:
                    cursor.execute("""
                        INSERT OR IGNORE INTO rounds_v3 (season_id, division_id, round_number, is_open, deadline)
                        SELECT 1, 1, round_number, is_open, deadline
                        FROM rounds
                    """)
                cursor.execute("DROP TABLE rounds")
                cursor.execute("ALTER TABLE rounds_v3 RENAME TO rounds")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_rounds_season_div_round ON rounds(season_id, division_id, round_number)")
                logger.info("Migrated 'rounds' table to composite (season_id, division_id, round_number) schema successfully.")
        except Exception as e:
            logger.exception(f"Error during rounds table migration check: {e}")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_rounds_season_div_round ON rounds(season_id, division_id, round_number)")

        # Migration: ensure round_reminders has composite PRIMARY KEY (division_id, round_number, reminder_type)
        try:
            cursor.execute("PRAGMA table_info(round_reminders)")
            rem_cols = cursor.fetchall()
            if rem_cols:
                rem_pk_cols = [c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in rem_cols if (c["pk"] if isinstance(c, sqlite3.Row) else c[5]) > 0]
                if "division_id" not in rem_pk_cols:
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS round_reminders_v2 (
                            division_id INTEGER NOT NULL DEFAULT 1,
                            round_number INTEGER NOT NULL,
                            reminder_type TEXT NOT NULL,
                            sent_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                            PRIMARY KEY(division_id, round_number, reminder_type)
                        )
                    """)
                    rem_col_names = [c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in rem_cols]
                    if "division_id" in rem_col_names:
                        cursor.execute("""
                            INSERT OR IGNORE INTO round_reminders_v2 (division_id, round_number, reminder_type, sent_at)
                            SELECT COALESCE(division_id, 1), round_number, reminder_type, sent_at
                            FROM round_reminders
                        """)
                    else:
                        cursor.execute("""
                            INSERT OR IGNORE INTO round_reminders_v2 (division_id, round_number, reminder_type, sent_at)
                            SELECT 1, round_number, reminder_type, sent_at
                            FROM round_reminders
                        """)
                    cursor.execute("DROP TABLE round_reminders")
                    cursor.execute("ALTER TABLE round_reminders_v2 RENAME TO round_reminders")
                    logger.info("Migrated 'round_reminders' table to composite (division_id, round_number, reminder_type) PK schema successfully.")
            else:
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS round_reminders (
                        division_id INTEGER NOT NULL DEFAULT 1,
                        round_number INTEGER NOT NULL,
                        reminder_type TEXT NOT NULL,
                        sent_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                        PRIMARY KEY(division_id, round_number, reminder_type)
                    )
                """)
        except Exception as e:
            logger.exception(f"Error during round_reminders table migration check: {e}")
        # Idempotency ledger for the auto-posted round preview / digest.
        # Deliberately NOT round_reminders: update_round_status() wipes that table
        # whenever a deadline is (re)set, which would re-post the same content.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS round_content_posts (
                division_id INTEGER NOT NULL,
                round_number INTEGER NOT NULL,
                content_type TEXT NOT NULL,
                message_id INTEGER,
                posted_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                PRIMARY KEY(division_id, round_number, content_type)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_drafts (
                draft_uuid TEXT PRIMARY KEY,
                draft_data TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        try:
            cursor.execute("""
                DELETE FROM pending_drafts
                WHERE created_at < datetime('now', '+3 hours', '-14 days')
            """)
        except Exception as e:
            logger.warning(f"Failed to prune old pending_drafts: {e}")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS debt_reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                stage TEXT NOT NULL,
                sent_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(match_id, stage),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        # Долг матча — одна строка на матч: срок, стадии трекера, итог.
        # Заменяет набор флагов debt_reminders (миграция 020 переносит их сюда).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS match_debts (
                match_id INTEGER PRIMARY KEY,
                division_id INTEGER,
                season_id INTEGER,
                round_number INTEGER,
                became_debt_at TEXT NOT NULL,
                grace_hours INTEGER NOT NULL DEFAULT 0,
                escalate_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'active',
                last_reminder_at TEXT,
                soft_warned_at TEXT,
                escalated_at TEXT,
                last_escalation_at TEXT,
                escalation_count INTEGER NOT NULL DEFAULT 0,
                global_escalated_at TEXT,
                resolved_at TEXT,
                resolution TEXT,
                resolved_by INTEGER,
                verdict_applied_at TEXT,
                reward_given_at TEXT,
                created_at TEXT DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_match_debts_state ON match_debts(state)")
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_warns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                admin_id INTEGER,
                reason TEXT,
                type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS pending_reports (
                match_id INTEGER PRIMARY KEY,
                reporter_id INTEGER NOT NULL,
                payload TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN warn_count INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE users ADD COLUMN squad_photo_id TEXT")
        except sqlite3.OperationalError:
            pass
            
        try:
            cursor.execute("ALTER TABLE matches ADD COLUMN is_extended INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE users ADD COLUMN pending_notification INTEGER DEFAULT 0")
        except sqlite3.OperationalError:
            pass
            
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cup_series (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage TEXT NOT NULL,
                series_num INTEGER NOT NULL,
                team1_name TEXT NOT NULL,
                team2_name TEXT NOT NULL,
                team1_wins INTEGER DEFAULT 0,
                team2_wins INTEGER DEFAULT 0,
                winner_name TEXT,
                status TEXT DEFAULT 'active'
            )
        """)

        # ─── Общий кубок: стадии ──────────────────────────────────────────────
        # Одна строка = один этап плей-офф на сезон. Имена колонок повторяют
        # `rounds` намеренно: предикат «принимать ли ставки» у лиги и кубка один
        # (`_evaluate_gate_row`), различается только то, где лежит строка-разрешение.
        # Стадия нигде не ограничивается CHECK-списком: `division_topics` показал,
        # как дорого потом вырезать из таблицы restrictive CHECK.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cup_stages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL DEFAULT 1,
                stage TEXT NOT NULL,
                stage_order INTEGER NOT NULL,
                is_open BOOLEAN NOT NULL DEFAULT 0,
                bets_open BOOLEAN NOT NULL DEFAULT 0,
                bets_opened_at TEXT,
                opened_at TEXT,
                opened_by INTEGER,
                deadline TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(season_id, stage)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cup_stages_season ON cup_stages(season_id, stage_order)")

        # ─── Кубки: темы «Кубок» ──────────────────────────────────────────────
        # Строка на (сезон, кубок): тема форума, куда бот пишет результаты кубка,
        # и закреплённая Pillow-сетка (`anchor_message_id`). Привязка — через
        # /set_div_topic <дивизион|общий> cup (см. set_cup_topic).
        # Отдельная таблица, а не строка в `division_topics`: там `division_id` —
        # FK на `divisions`, а кубок дивизионом не является. Синтетический
        # «дивизион КУБОК» притащил бы его в 11 дивизионные читалки и во вкладки
        # Mini App; здесь кубковая тема не может просочиться ни в один пикер лиги.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS cup_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL DEFAULT 1,
                topic_type TEXT NOT NULL,
                group_chat_id INTEGER NOT NULL,
                message_thread_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(season_id, topic_type),
                FOREIGN KEY(season_id) REFERENCES seasons(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_cup_topics_chat ON cup_topics(group_chat_id, message_thread_id)")
        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES (?, 'Общий кубок: таблица cup_topics (темы вещания этапа)')
        """, (MIGRATION_023_CUP_TOPICS,))
        # anchor_message_id — закреплённое в теме сообщение с сеткой кубка. Строки
        # прошлой схемы (вещание ответами под пост, message_thread_id = 0) темой
        # не считаются: get_cup_topic их пропускает.
        try:
            cursor.execute("ALTER TABLE cup_topics ADD COLUMN anchor_message_id INTEGER")
        except sqlite3.OperationalError:
            pass
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_cup_topics_anchor ON cup_topics(group_chat_id, anchor_message_id)"
        )

        # Safely migration-add new columns to matches using predefined SAFE_COLUMNS tuple.
        # Note: String interpolation is safe here as column names/types are hardcoded internal constants, not user input.
        SAFE_COLUMNS = (
            ("photo_id", "TEXT"),
            ("dispute_photos", "TEXT"),
            ("reported_by", "INTEGER"),
            ("proposed_time", "TEXT"),
            ("proposed_by", "INTEGER"),
            ("time_status", "TEXT DEFAULT 'none'"),
            ("tournament_type", "TEXT DEFAULT 'league'"),
            ("cup_stage", "TEXT"),
            ("cup_series_id", "INTEGER"),
            ("game_num_in_series", "INTEGER DEFAULT 1"),
            ("player1_team", "TEXT"),
            ("player2_team", "TEXT"),
            ("frozen_seconds", "INTEGER DEFAULT 0"),
            ("frozen_at", "TEXT"),
            ("ht_score1", "INTEGER"),
            ("ht_score2", "INTEGER"),
            ("match_date", "TEXT"),
            ("match_time", "TEXT"),
            ("stadium", "TEXT"),
            ("referee", "TEXT"),
            ("live_minute", "INTEGER"),
            ("division_id", "INTEGER DEFAULT NULL"),
            ("season_id", "INTEGER NOT NULL DEFAULT 1"),
            # Technical results (ТП / ТН): the score was assigned by an admin,
            # not played. Bets on such a match are always fully refunded.
            ("is_technical", "INTEGER DEFAULT 0"),
            ("technical_type", "TEXT DEFAULT NULL"),
            # Admin extension of a debt match (+24h / +48h): the moment the
            # extension expires and the debt clock resumes.
            ("extended_until", "TEXT DEFAULT NULL"),
            # Общий кубок: этап (`cup_stages.id`) и клуб, прошедший дальше.
            # Последний — именно поле, а не производная от счёта: в кубке не бывает
            # ничьей, а 2:2 в основное время разрешается послематчевыми, поэтому
            # победителя игры нельзя вывести из player1_score/player2_score.
            ("stage_id", "INTEGER"),
            ("cup_winner_team", "TEXT"),
            # Строка-заголовок серии: мета-объект «серия целиком», а не игра. Живёт
            # в `matches`, чтобы рынки на серию («кто проходит», «счёт серии»,
            # «будет ли третья игра») обслуживались теми же `markets`, `bet_items`
            # и `settlement_engine`, что и рынки на матч: «счёт» заголовка — победы
            # в серии. Каждому читателю реальных игр такая строка не видна.
            ("is_series_header", "INTEGER NOT NULL DEFAULT 0"),
        )
        for col_name, col_type in SAFE_COLUMNS:
            try:
                cursor.execute(f"ALTER TABLE matches ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_season_div ON matches(season_id, division_id)")

        # 👑 Игрок матча (MVP), распознанный по золотой короне на скриншоте.
        # Хранится именем, а не ссылкой на squad_players: OCR может увидеть игрока
        # раньше, чем состав заявлен в боте (та же логика, что у match_events).
        cursor.execute("PRAGMA table_info(matches)")
        _match_cols = [row[1] for row in cursor.fetchall()]
        if "mvp_player" not in _match_cols:
            cursor.execute("ALTER TABLE matches ADD COLUMN mvp_player TEXT DEFAULT NULL")
        # Индекс создаётся отдельно от ALTER: на базе, где колонка уже добавлена
        # предыдущей версией миграции, индекса иначе не появилось бы никогда.
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_matches_mvp_player "
            "ON matches(mvp_player) WHERE mvp_player IS NOT NULL"
        )

        # Safely migration-add division_id to users and rounds
        try:
            cursor.execute("ALTER TABLE users ADD COLUMN division_id INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        try:
            cursor.execute("ALTER TABLE rounds ADD COLUMN division_id INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        # Ранняя линия Logovo.bet: приём прогнозов может открываться до того,
        # как тур открыт для игры. `bets_open` независим от `is_open`.
        for col_name, col_type in (
            ("bets_open", "BOOLEAN DEFAULT 0"),
            ("bets_opened_at", "TEXT"),
        ):
            try:
                cursor.execute(f"ALTER TABLE rounds ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Жизненный цикл тура: scheduled → open → closed (см. services.debt_policy).
        # `is_open` остаётся авторитетным для «открыт ли тур»; `status` различает
        # «ещё не открывали» и «закрыт», `closed_at` нужен для срока долга.
        for col_name, col_type in (
            ("status", "TEXT"),
            ("closed_at", "TEXT"),
            ("closed_by", "INTEGER"),
        ):
            try:
                cursor.execute(f"ALTER TABLE rounds ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        # Инвариант линии: тур, открытый для игры, ставки не принимает.
        # Состояние is_open = 1 AND bets_open = 1 недопустимо; ранний бэкфилл
        # этой колонки его создавал — нормализуем один раз.
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '011_round_betting_cutoff'")
        if not cursor.fetchone():
            cursor.execute("UPDATE rounds SET bets_open = 0, bets_opened_at = NULL WHERE is_open = 1")
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('011_round_betting_cutoff', 'Round betting cutoff: is_open=1 implies bets_open=0')
            """)

        # Safely migration-add new columns to user_bets, bet_items, coin_transactions, user_wallets
        for col_name, col_type in (
            ("system_config", "TEXT"),
            ("actual_payout", "INTEGER DEFAULT 0"),
            ("idempotency_key", "TEXT"),
            ("cashout_at", "TIMESTAMP"),
        ):
            try:
                cursor.execute(f"ALTER TABLE user_bets ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        for col_name, col_type in (
            ("market_id", "INTEGER"),
            ("selection_id", "INTEGER"),
            ("odds_at_placement", "REAL"),
        ):
            try:
                cursor.execute(f"ALTER TABLE bet_items ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        for col_name, col_type in (
            ("reference_type", "TEXT"),
            ("balance_after", "INTEGER"),
        ):
            try:
                cursor.execute(f"ALTER TABLE coin_transactions ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        try:
            cursor.execute("ALTER TABLE user_wallets ADD COLUMN daily_limit INTEGER")
        except sqlite3.OperationalError:
            pass

        # Safely migrate user_bets CHECK constraint to include 'cashed_out' (LB-18)
        try:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='user_bets'")
            ub_row = cursor.fetchone()
            if ub_row and ub_row[0] and "cashed_out" not in ub_row[0]:
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("""
                    CREATE TABLE user_bets_migrate_cashed_out (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        bet_type TEXT NOT NULL DEFAULT 'single' CHECK(bet_type IN ('single', 'express')),
                        amount INTEGER NOT NULL,
                        total_odd REAL NOT NULL,
                        potential_win INTEGER NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'lost', 'refunded', 'cancelled', 'cashed_out')),
                        system_config TEXT,
                        actual_payout INTEGER DEFAULT 0,
                        idempotency_key TEXT,
                        idempotency_payload_hash TEXT,
                        cashout_at TIMESTAMP,
                        created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                        settled_at TIMESTAMP
                    )
                """)
                cursor.execute("""
                    INSERT INTO user_bets_migrate_cashed_out (
                        id, user_id, bet_type, amount, total_odd, potential_win, status,
                        system_config, actual_payout, idempotency_key, idempotency_payload_hash,
                        cashout_at, created_at, settled_at
                    )
                    SELECT id, user_id, bet_type, amount, total_odd, potential_win, status,
                           system_config, actual_payout, idempotency_key, idempotency_payload_hash,
                           cashout_at, created_at, settled_at
                    FROM user_bets
                """)
                cursor.execute("DROP TABLE user_bets")
                cursor.execute("ALTER TABLE user_bets_migrate_cashed_out RENAME TO user_bets")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id, status)")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_status ON user_bets(status)")
                cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_bets_idempotency ON user_bets(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL")
                cursor.execute("PRAGMA foreign_keys=ON")
        except Exception as e:
            logger.warning(f"Could not migrate user_bets check constraint: {e}")
            
        # Performance indexes for matches and match_events
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_p1 ON matches(player1_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_p2 ON matches(player2_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_round ON matches(round_number)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_tourn ON matches(tournament_type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_matches_division ON matches(division_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_division ON users(division_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_events_match ON match_events(match_id)")

        # Enforce one owner per club: de-duplicate team_name (prefer a real
        # positive telegram_id over a temporary negative one) then add a UNIQUE
        # index. Prevents duplicated cup debt rows and warns being attributed
        # to the wrong duplicate account.
        cursor.execute("""
            UPDATE users SET team_name = NULL
            WHERE team_name IS NOT NULL AND telegram_id NOT IN (
                SELECT MAX(telegram_id) FROM users WHERE team_name IS NOT NULL GROUP BY LOWER(TRIM(team_name))
            )
        """)
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_team_name_unique ON users(LOWER(TRIM(team_name)))"
        )

        # Persistent chat history for AI mode
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS chat_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_user ON chat_history(user_id, id)")

        # Style samples for AI persona learning (real messages from a source user)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS style_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)

        # High-performance Telegram file_id deduplication & media caching
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS telegram_media_cache (
                file_hash TEXT PRIMARY KEY,
                file_id TEXT NOT NULL,
                media_type TEXT NOT NULL DEFAULT 'animation',
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_media_cache_type ON telegram_media_cache(media_type)")

        # ─── Logovo.bet: Virtual Prediction & Betting System ───
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_wallets (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 677,
                total_wagered INTEGER NOT NULL DEFAULT 0,
                total_won INTEGER NOT NULL DEFAULT 0,
                bets_count INTEGER NOT NULL DEFAULT 0,
                bets_won INTEGER NOT NULL DEFAULT 0,
                daily_limit INTEGER,
                last_bonus_at TEXT,
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_wallets_balance ON user_wallets(balance DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bet_markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL UNIQUE,
                tour INTEGER NOT NULL,
                team1_name TEXT NOT NULL,
                team2_name TEXT NOT NULL,
                odd_p1 REAL NOT NULL,
                odd_x REAL NOT NULL,
                odd_p2 REAL NOT NULL,
                odd_tb25 REAL NOT NULL DEFAULT 1.80,
                odd_tm25 REAL NOT NULL DEFAULT 1.95,
                odd_btts_yes REAL NOT NULL DEFAULT 1.70,
                odd_btts_no REAL NOT NULL DEFAULT 2.05,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_markets_tour ON bet_markets(tour, is_active)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_markets_match ON bet_markets(match_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_bets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                bet_type TEXT NOT NULL DEFAULT 'single' CHECK(bet_type IN ('single', 'express')),
                amount INTEGER NOT NULL,
                total_odd REAL NOT NULL,
                potential_win INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'lost', 'refunded', 'cancelled', 'cashed_out')),
                system_config TEXT,
                actual_payout INTEGER DEFAULT 0,
                idempotency_key TEXT,
                idempotency_payload_hash TEXT,
                cashout_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                settled_at TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_user ON user_bets(user_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_status ON user_bets(status)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_bets_idempotency ON user_bets(user_id, idempotency_key) WHERE idempotency_key IS NOT NULL")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bet_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                outcome_type TEXT NOT NULL,
                odd REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'won', 'lost', 'refunded')),
                market_id INTEGER,
                selection_id INTEGER,
                odds_at_placement REAL,
                FOREIGN KEY(bet_id) REFERENCES user_bets(id) ON DELETE CASCADE,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_items_bet ON bet_items(bet_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_items_match ON bet_items(match_id, status)")
        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bet_items_bet_match ON bet_items(bet_id, match_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS coin_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                transaction_type TEXT NOT NULL,
                reference_id INTEGER,
                reference_type TEXT,
                balance_after INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_coin_tx_user ON coin_transactions(user_id, created_at)")

        # ─── Logovo.bet: Relational Markets & Extended Prediction Schema ───
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                short_name TEXT,
                logo_url TEXT,
                owner_id INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(owner_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_teams_name ON teams(LOWER(name))")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tournaments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'league' CHECK(type IN ('league', 'cup', 'friendly')),
                season TEXT,
                is_active BOOLEAN DEFAULT 1,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO tournaments (id, name, type, season, is_active, created_at)
            VALUES (1, 'Логово Фифарей (Основная Лига)', 'league', 'Сезон 2026', 1, datetime('now', '+3 hours'))
        """)

        # ─── LOGOVO: Divisions & Multi-Topic Routing Tables ───
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS divisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tournament_id INTEGER NOT NULL DEFAULT 1,
                name TEXT NOT NULL,
                code TEXT NOT NULL UNIQUE,
                season_id INTEGER DEFAULT NULL,
                topic_id INTEGER DEFAULT NULL,
                group_chat_id INTEGER DEFAULT NULL,
                is_active BOOLEAN DEFAULT 1,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(tournament_id) REFERENCES tournaments(id),
                FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE SET NULL
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_divisions_active ON divisions(is_active, sort_order)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_divisions_topic ON divisions(topic_id)")

        try:
            cursor.execute("PRAGMA table_info(divisions)")
            d_cols = cursor.fetchall()
            d_col_names = [c["name"] if isinstance(c, sqlite3.Row) else c[1] for c in d_cols]
            if "season_id" not in d_col_names:
                cursor.execute("ALTER TABLE divisions ADD COLUMN season_id INTEGER NOT NULL DEFAULT 1")
            if "group_chat_id" not in d_col_names:
                cursor.execute("ALTER TABLE divisions ADD COLUMN group_chat_id INTEGER DEFAULT NULL")
        except Exception as e:
            logger.debug(f"Notice during divisions migration: {e}")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_divisions_season ON divisions(season_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_divisions_group ON divisions(group_chat_id)")

        cursor.execute("""
            INSERT OR IGNORE INTO divisions (id, tournament_id, name, code, season_id, sort_order, created_at)
            VALUES 
                (1, 1, 'Дивизион 1', 'DIV_1', 1, 1, datetime('now', '+3 hours')),
                (2, 1, 'Дивизион 2', 'DIV_2', 1, 2, datetime('now', '+3 hours')),
                (3, 1, 'Дивизион 3', 'DIV_3', 1, 3, datetime('now', '+3 hours')),
                (4, 1, 'Дивизион 4', 'DIV_4', 1, 4, datetime('now', '+3 hours')),
                (5, 1, 'Дивизион 5', 'DIV_5', 1, 5, datetime('now', '+3 hours'))
        """)

        # INSERT OR IGNORE выше молча пропускает уже существующие строки, поэтому
        # дивизион, заведённый руками раньше сида, живёт со своим кодом — а по коду
        # ищется сезонный состав клубов. Разовая починка для таких баз.
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '012_canonical_division_codes'")
        if not cursor.fetchone():
            repair_canonical_division_codes()
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('012_canonical_division_codes', 'Divisions 1-5 carry the canonical DIV_1..DIV_5 codes')
            """)

        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES ('003_p2_seasons_and_isolation', 'Phase 2: Season entity, lifecycle, and strict isolation')
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS division_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                division_id INTEGER NOT NULL,
                topic_type TEXT NOT NULL,
                message_thread_id INTEGER NOT NULL,
                group_chat_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(division_id, topic_type),
                FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_div_topics_lookup ON division_topics(message_thread_id, topic_type)")
        
        try:
            cursor.execute("ALTER TABLE division_topics ADD COLUMN group_chat_id INTEGER DEFAULT NULL")
        except sqlite3.OperationalError:
            pass

        # Check if division_topics has restrictive CHECK constraint
        cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='division_topics'")
        dt_schema = cursor.fetchone()
        if dt_schema and dt_schema["sql"] and "CHECK(topic_type IN" in dt_schema["sql"]:
            try:
                cursor.execute("PRAGMA foreign_keys=OFF")
                cursor.execute("""
                    CREATE TABLE division_topics__migration (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        division_id INTEGER NOT NULL,
                        topic_type TEXT NOT NULL,
                        message_thread_id INTEGER NOT NULL,
                        group_chat_id INTEGER DEFAULT NULL,
                        created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                        UNIQUE(division_id, topic_type),
                        FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE
                    )
                """)
                cursor.execute("PRAGMA table_info(division_topics)")
                cols = [c["name"] for c in cursor.fetchall()]
                if "group_chat_id" in cols:
                    cursor.execute("""
                        INSERT INTO division_topics__migration (id, division_id, topic_type, message_thread_id, group_chat_id, created_at)
                        SELECT id, division_id, topic_type, message_thread_id, group_chat_id, created_at FROM division_topics
                    """)
                else:
                    cursor.execute("""
                        INSERT INTO division_topics__migration (id, division_id, topic_type, message_thread_id, created_at)
                        SELECT id, division_id, topic_type, message_thread_id, created_at FROM division_topics
                    """)
                cursor.execute("UPDATE division_topics__migration SET topic_type = 'draft' WHERE topic_type = 'drafts'")
                cursor.execute("DROP TABLE division_topics")
                cursor.execute("ALTER TABLE division_topics__migration RENAME TO division_topics")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_div_topics_lookup ON division_topics(message_thread_id, topic_type)")
            except Exception as e:
                logger.warning(f"Error migrating division_topics check constraint: {e}")

        cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_div_topics_chat_thread ON division_topics(group_chat_id, message_thread_id) WHERE group_chat_id IS NOT NULL")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS division_admins (
                division_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                PRIMARY KEY(division_id, user_id),
                FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_div_admins_user ON division_admins(user_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS markets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                market_key TEXT NOT NULL,
                market_name TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'main',
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','suspended','closed','settled','voided')),
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(match_id, market_key),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_markets_match ON markets(match_id, status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_markets_key ON markets(market_key)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS market_selections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                market_id INTEGER NOT NULL,
                selection_key TEXT NOT NULL,
                selection_name TEXT NOT NULL,
                odds_value REAL NOT NULL,
                odds_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','locked','voided')),
                previous_odds REAL,
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(market_id, selection_key),
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_selections_market ON market_selections(market_id, status)")
        # Last raw (unsmoothed) model price: repricing steps toward it at most
        # ±15% per model change, so re-fetching the line does not keep stepping.
        try:
            cursor.execute("ALTER TABLE market_selections ADD COLUMN model_odds REAL")
        except sqlite3.OperationalError:
            pass  # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                selection_id INTEGER NOT NULL,
                old_value REAL,
                new_value REAL NOT NULL,
                changed_by INTEGER,
                reason TEXT,
                changed_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(selection_id) REFERENCES market_selections(id) ON DELETE CASCADE,
                FOREIGN KEY(changed_by) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_hist_sel ON odds_history(selection_id, changed_at DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                target_type TEXT NOT NULL CHECK(target_type IN ('match','team','tournament')),
                target_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(user_id, target_type, target_id),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id, target_type)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                reference_id INTEGER,
                is_read BOOLEAN DEFAULT 0,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, is_read, created_at DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_notification_settings (
                user_id INTEGER NOT NULL,
                notification_type TEXT NOT NULL,
                is_enabled BOOLEAN NOT NULL DEFAULT 1,
                PRIMARY KEY(user_id, notification_type),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id INTEGER,
                old_value TEXT,
                new_value TEXT,
                division_id INTEGER DEFAULT NULL,
                season_id INTEGER DEFAULT NULL,
                reason TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(admin_id) REFERENCES users(telegram_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_admin ON admin_audit_log(admin_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_target ON admin_audit_log(target_type, target_id)")
        for col_name, col_type in (
            ("division_id", "INTEGER DEFAULT NULL"),
            ("season_id", "INTEGER DEFAULT NULL"),
            # services/odds_engine.py writes this column on market suspend/unsuspend.
            ("reason", "TEXT DEFAULT NULL"),
        ):
            try:
                cursor.execute(f"ALTER TABLE admin_audit_log ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass

        # ─── Phase 5: Betting Audit Log ───────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS bet_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                entity_id INTEGER NOT NULL,
                old_value TEXT,
                new_value TEXT,
                division_id INTEGER,
                season_id INTEGER,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_audit_actor ON bet_audit_log(actor_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_audit_entity ON bet_audit_log(entity_type, entity_id)")

        # ─── Phase 5: Idempotency payload hash migration ──────────────────────
        try:
            cursor.execute("ALTER TABLE user_bets ADD COLUMN idempotency_payload_hash TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Потолок выигрыша 10 000 / открытых ставок 35 000 действует только для
        # новых купонов. Всё, что уже лежит в user_bets на момент выкатки, помечается
        # legacy_limits = 1: такие ставки рассчитываются как раньше и не занимают
        # лимит открытой ответственности игрока. Новые купоны получают DEFAULT 0.
        try:
            cursor.execute("ALTER TABLE user_bets ADD COLUMN legacy_limits INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '014_payout_cap_legacy_bets'")
        if not cursor.fetchone():
            cursor.execute("UPDATE user_bets SET legacy_limits = 1")
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('014_payout_cap_legacy_bets', 'Bets placed before the 10k payout cap keep the old limits')
            """)

        # Надбавка на экспресс, действовавшая при приёме купона (см. express_odd).
        # NULL — купон принят до её введения и рассчитывается по чистому кэфу.
        try:
            cursor.execute("ALTER TABLE user_bets ADD COLUMN express_margin_pct INTEGER")
        except sqlite3.OperationalError:
            pass  # Column already exists

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS saved_coupons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                name TEXT,
                selections_json TEXT NOT NULL,
                total_odd REAL NOT NULL,
                status TEXT DEFAULT 'active' CHECK(status IN ('active','expired','updated')),
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_saved_user ON saved_coupons(user_id, status)")

        # ─── Logovo.bet: Gamification, Progression, Quests & Social Tables ───
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_progression (
                user_id INTEGER PRIMARY KEY,
                level INTEGER NOT NULL DEFAULT 1,
                current_xp INTEGER NOT NULL DEFAULT 0,
                total_xp_earned INTEGER NOT NULL DEFAULT 0,
                current_streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0,
                login_streak INTEGER NOT NULL DEFAULT 0,
                best_login_streak INTEGER NOT NULL DEFAULT 0,
                last_active_date TEXT,
                streak_shields INTEGER NOT NULL DEFAULT 1,
                equipped_frame TEXT NOT NULL DEFAULT 'default',
                equipped_title TEXT NOT NULL DEFAULT 'Новичок',
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_progression_level ON user_progression(level DESC, current_xp DESC)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS achievements_catalog (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                category TEXT NOT NULL,
                rarity TEXT NOT NULL,
                reward_xp INTEGER NOT NULL DEFAULT 100,
                reward_coins INTEGER NOT NULL DEFAULT 250,
                badge_icon TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)

        # ── 016: каталог не умеет удалять строки ────────────────────────────
        # seed_gamification_catalog() делает upsert и ничего не чистит, поэтому
        # достижения снятых механик (дуэли PvP убраны в v2.0) навсегда остаются
        # в знаменателе «получено N из M» и никогда не могут быть получены.
        # ACH_HOT_STREAK — дубль ACH_STREAK_5 (то же условие, серия из 5 побед),
        # который платил награду дважды за одно и то же. Строки не удаляются:
        # у кого-то они уже открыты, и FK из user_achievements должен остаться
        # живым — достижение просто уходит из активного каталога.
        try:
            cursor.execute("ALTER TABLE achievements_catalog ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        except sqlite3.OperationalError:
            pass  # Column already exists
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '016_retire_dead_achievements'")
        if not cursor.fetchone():
            cursor.execute("""
                UPDATE achievements_catalog
                SET is_active = 0
                WHERE id IN ('ACH_DUEL_FIRST', 'ACH_DUEL_5_WINS', 'ACH_HOT_STREAK')
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('016_retire_dead_achievements', 'Retire PvP duel achievements and the ACH_STREAK_5 duplicate')
            """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_achievements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                achievement_id TEXT NOT NULL,
                is_claimed BOOLEAN NOT NULL DEFAULT 0,
                unlocked_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(user_id, achievement_id),
                FOREIGN KEY (achievement_id) REFERENCES achievements_catalog(id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_achievements_uid ON user_achievements(user_id)")

        # ── 015: серия входов и серия побед — это два разных счётчика ────────
        # `current_streak` принадлежит StreakEngine и считает подряд выигранные
        # ставки; `login_streak` принадлежит check_and_update_login_streak и
        # считает дни подряд. Раньше колонка была одна на двоих, поэтому серия
        # побед читалась как дни и выдавала ACH_LOGIN_3 тому, кто не заходил три
        # дня подряд. Бэкфилл честно не восстановить — прошлых дат входа нет,
        # поэтому активным ставится 1 день, а невыданные (is_claimed = 0) награды
        # за вход отзываются, чтобы их заработали заново.
        try:
            cursor.execute("ALTER TABLE user_progression ADD COLUMN login_streak INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            cursor.execute("ALTER TABLE user_progression ADD COLUMN best_login_streak INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass  # Column already exists
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '015_split_login_and_win_streaks'")
        if not cursor.fetchone():
            cursor.execute("""
                UPDATE user_progression
                SET login_streak = CASE WHEN last_active_date IS NULL THEN 0 ELSE 1 END,
                    best_login_streak = CASE WHEN last_active_date IS NULL THEN 0 ELSE 1 END
            """)
            cursor.execute("""
                DELETE FROM user_achievements
                WHERE achievement_id IN ('ACH_LOGIN_3', 'ACH_LOGIN_7', 'ACH_LOGIN_30')
                  AND is_claimed = 0
            """)
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('015_split_login_and_win_streaks', 'login_streak split off current_streak; unclaimed ACH_LOGIN_* revoked')
            """)

        # (quests_catalog, user_quests, pvp_duels tables removed in v2.0 cleanup)

        # ─── Phase 6: Live Match States ──────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS live_match_states (
                match_id INTEGER PRIMARY KEY,
                season_id INTEGER NOT NULL DEFAULT 1,
                division_id INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'SCHEDULED',
                period TEXT NOT NULL DEFAULT 'pre_match',
                minute INTEGER,
                home_score INTEGER NOT NULL DEFAULT 0,
                away_score INTEGER NOT NULL DEFAULT 0,
                provider TEXT NOT NULL DEFAULT 'none',
                provider_match_id TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                last_updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_live_states_status ON live_match_states(status)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_live_states_div_season ON live_match_states(division_id, season_id)")

        # ─── Phase 6: Live Events ────────────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS live_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                minute INTEGER NOT NULL,
                added_time INTEGER,
                team_id INTEGER,
                team_name TEXT,
                player_id INTEGER,
                player_name TEXT,
                payload TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
                UNIQUE(provider, provider_event_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_live_events_match ON live_events(match_id, minute)")

        # ─── Phase 6: Live Statistics (NULL = unavailable) ───────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS live_statistics (
                match_id INTEGER PRIMARY KEY,
                possession_home REAL,
                possession_away REAL,
                shots_home INTEGER,
                shots_away INTEGER,
                shots_on_target_home INTEGER,
                shots_on_target_away INTEGER,
                corners_home INTEGER,
                corners_away INTEGER,
                fouls_home INTEGER,
                fouls_away INTEGER,
                offsides_home INTEGER,
                offsides_away INTEGER,
                yellow_cards_home INTEGER,
                yellow_cards_away INTEGER,
                red_cards_home INTEGER,
                red_cards_away INTEGER,
                dangerous_attacks_home INTEGER,
                dangerous_attacks_away INTEGER,
                attacks_home INTEGER,
                attacks_away INTEGER,
                passes_home INTEGER,
                passes_away INTEGER,
                pass_accuracy_home REAL,
                pass_accuracy_away REAL,
                xg_home REAL,
                xg_away REAL,
                saves_home INTEGER,
                saves_away INTEGER,
                substitutions_home INTEGER,
                substitutions_away INTEGER,
                provider TEXT NOT NULL DEFAULT 'none',
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)

        # ─── Phase 6: Odds Movement Tracking ─────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS odds_movement (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                selection_id INTEGER NOT NULL,
                market_id INTEGER NOT NULL,
                match_id INTEGER NOT NULL,
                old_odds REAL NOT NULL,
                new_odds REAL NOT NULL,
                pct_change REAL NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('up', 'down', 'neutral')),
                velocity REAL NOT NULL DEFAULT 0.0,
                reason TEXT,
                source TEXT NOT NULL DEFAULT 'system',
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(selection_id) REFERENCES market_selections(id) ON DELETE CASCADE,
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE CASCADE,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_mov_sel ON odds_movement(selection_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_mov_match ON odds_movement(match_id, created_at DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_odds_mov_created ON odds_movement(created_at DESC)")

        # ─── Phase 6: Notification Events (Deduplicated) ─────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS notification_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                link TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                sent_at TIMESTAMP,
                UNIQUE(user_id, event_type, source_event_id),
                FOREIGN KEY(user_id) REFERENCES users(telegram_id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_notif_events_user ON notification_events(user_id, status, created_at DESC)")

        # ─── Phase 6: Provider Sync State ─────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_sync_state (
                provider TEXT PRIMARY KEY,
                last_sync_at TIMESTAMP,
                status TEXT NOT NULL DEFAULT 'idle',
                error_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)

        # ─── Phase 7: Team Elo Ratings ───────────────────────────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS team_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                team_name TEXT NOT NULL,
                division_id INTEGER NOT NULL DEFAULT 1,
                season_id INTEGER NOT NULL DEFAULT 1,
                elo_rating REAL NOT NULL DEFAULT 1500.0,
                matches_counted INTEGER NOT NULL DEFAULT 0,
                last_updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(team_name, division_id, season_id),
                FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE,
                FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_team_ratings_div ON team_ratings(division_id, season_id)")

        # ─── Phase 7: AI Predictions with Versioning & Resolution Tracking ───
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                division_id INTEGER NOT NULL DEFAULT 1,
                season_id INTEGER NOT NULL DEFAULT 1,
                model_version TEXT NOT NULL DEFAULT 'ensemble_v1',
                feature_version TEXT NOT NULL DEFAULT 'features_v1',
                home_probability REAL NOT NULL,
                draw_probability REAL NOT NULL,
                away_probability REAL NOT NULL,
                over_1_5_probability REAL,
                over_2_5_probability REAL,
                over_3_5_probability REAL,
                btts_yes_probability REAL,
                btts_no_probability REAL,
                confidence REAL NOT NULL DEFAULT 0.5,
                key_factors TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                resolved_at TIMESTAMP,
                actual_result TEXT,
                is_correct BOOLEAN,
                brier_score REAL,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
                FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE,
                FOREIGN KEY(season_id) REFERENCES seasons(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_predictions_match ON predictions(match_id, model_version)")
        # Уникальность по этим же колонкам появляется не здесь, а миграцией 019:
        # на развёрнутой базе этот индекс исторически не уникальный и дубли в
        # predictions уже есть, так что CREATE UNIQUE INDEX прямо в DDL оборвал бы
        # init_db() на каждом старте.
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_predictions_div_season ON predictions(division_id, season_id, created_at DESC)")

        # ─── Phase 7: Live & Pre-Match Prediction Snapshots ───────────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS prediction_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER NOT NULL,
                stage TEXT NOT NULL CHECK(stage IN ('PRE_MATCH', 'LIVE', 'FINAL')),
                minute INTEGER,
                home_score INTEGER NOT NULL DEFAULT 0,
                away_score INTEGER NOT NULL DEFAULT 0,
                home_prob REAL NOT NULL,
                draw_prob REAL NOT NULL,
                away_prob REAL NOT NULL,
                confidence REAL NOT NULL,
                snapshot_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_pred_snapshots_match ON prediction_snapshots(match_id, snapshot_at DESC)")

        # ─── Phase 8: Real Sports Provider Integration & Telemetry ────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS sports_providers (
                provider_name TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                is_active BOOLEAN NOT NULL DEFAULT 1,
                base_url TEXT,
                rate_limit_rpm INTEGER DEFAULT 60,
                circuit_breaker_status TEXT DEFAULT 'CLOSED',
                consecutive_failures INTEGER DEFAULT 0,
                last_sync_at TIMESTAMP,
                last_error TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_matches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                provider_match_id TEXT NOT NULL,
                match_id INTEGER NOT NULL,
                division_id INTEGER NOT NULL DEFAULT 1,
                season_id INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'SCHEDULED',
                home_score INTEGER NOT NULL DEFAULT 0,
                away_score INTEGER NOT NULL DEFAULT 0,
                minute INTEGER,
                last_update_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                payload TEXT,
                UNIQUE(provider, provider_match_id),
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_matches_lookup ON provider_matches(provider, provider_match_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_matches_match ON provider_matches(match_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_matches_update ON provider_matches(last_update_at)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS provider_sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                status_code INTEGER,
                records_count INTEGER DEFAULT 0,
                latency_ms REAL DEFAULT 0.0,
                error_message TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_provider_sync_log_provider ON provider_sync_log(provider, created_at DESC)")

        # ─── Phase 9: Risk Engine, Centralized Limits & Alerts ──────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS risk_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                severity TEXT NOT NULL CHECK(severity IN ('low', 'medium', 'high', 'critical')),
                division_id INTEGER,
                match_id INTEGER,
                market_id INTEGER,
                selection_id INTEGER,
                message TEXT NOT NULL,
                details_json TEXT,
                status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'acknowledged', 'resolved')),
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                resolved_at TIMESTAMP,
                FOREIGN KEY(division_id) REFERENCES divisions(id) ON DELETE CASCADE,
                FOREIGN KEY(match_id) REFERENCES matches(id) ON DELETE CASCADE,
                FOREIGN KEY(market_id) REFERENCES markets(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_risk_alerts_status ON risk_alerts(status, severity)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_risk_alerts_div ON risk_alerts(division_id, created_at)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS risk_limits_config (
                scope_type TEXT NOT NULL,
                scope_id INTEGER NOT NULL DEFAULT 0,
                limit_key TEXT NOT NULL,
                limit_value REAL NOT NULL,
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                PRIMARY KEY(scope_type, scope_id, limit_key)
            )
        """)

        # Performance indexes for risk and exposure aggregations
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_user_bets_exposure ON user_bets(status, settled_at)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_bet_items_exposure ON bet_items(market_id, selection_id, status)")

        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES ('009_phase9_risk_and_limits', 'Phase 9: Production Betting Intelligence, Risk Engine & Alerts')
        """)

        # ─── Phase 10: Economy, Ranking & Seasonal Progression ──────────────
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS season_player_stats (
                user_id INTEGER NOT NULL,
                season_id INTEGER NOT NULL DEFAULT 1,
                division_id INTEGER NOT NULL DEFAULT 1,
                rating REAL NOT NULL DEFAULT 1200.0,
                confidence REAL NOT NULL DEFAULT 350.0,
                season_points REAL NOT NULL DEFAULT 0.0,
                total_bets INTEGER NOT NULL DEFAULT 0,
                settled_bets INTEGER NOT NULL DEFAULT 0,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                voids INTEGER NOT NULL DEFAULT 0,
                win_rate REAL NOT NULL DEFAULT 0.0,
                roi REAL NOT NULL DEFAULT 0.0,
                total_stake INTEGER NOT NULL DEFAULT 0,
                total_payout INTEGER NOT NULL DEFAULT 0,
                current_streak INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0,
                value_bets_hit INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE', 'QUALIFYING', 'INACTIVE')),
                rank INTEGER DEFAULT NULL,
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                PRIMARY KEY(user_id, season_id, division_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sps_leaderboard ON season_player_stats(season_id, division_id, rating DESC, season_points DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_sps_user ON season_player_stats(user_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS season_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                division_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                final_rank INTEGER NOT NULL,
                final_rating REAL NOT NULL,
                season_points REAL NOT NULL,
                wins INTEGER NOT NULL,
                losses INTEGER NOT NULL,
                voids INTEGER NOT NULL DEFAULT 0,
                settled_bets INTEGER NOT NULL,
                win_rate REAL NOT NULL,
                roi REAL NOT NULL,
                total_stake INTEGER NOT NULL DEFAULT 0,
                total_payout INTEGER NOT NULL DEFAULT 0,
                best_streak INTEGER NOT NULL DEFAULT 0,
                promotion_status TEXT NOT NULL CHECK(promotion_status IN ('PROMOTED', 'RELEGATED', 'STAY', 'INACTIVE')),
                rewards_json TEXT DEFAULT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(season_id, division_id, user_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_season_div ON season_snapshots(season_id, division_id, final_rank)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_snapshots_user ON season_snapshots(user_id)")

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS season_rules_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                division_id INTEGER NOT NULL,
                promotion_slots INTEGER NOT NULL DEFAULT 3,
                relegation_slots INTEGER NOT NULL DEFAULT 3,
                min_bets_qualification INTEGER NOT NULL DEFAULT 5,
                min_matches_qualification INTEGER NOT NULL DEFAULT 3,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(season_id, division_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS season_rewards_catalog (
                id TEXT PRIMARY KEY,
                season_id INTEGER,
                division_id INTEGER,
                name TEXT NOT NULL,
                reward_type TEXT NOT NULL CHECK(reward_type IN ('coins', 'xp', 'badge', 'title')),
                amount INTEGER NOT NULL DEFAULT 0,
                badge_id TEXT DEFAULT NULL,
                title TEXT DEFAULT NULL,
                criteria TEXT NOT NULL CHECK(criteria IN ('CHAMPION', 'TOP_3', 'TOP_10', 'PROMOTION', 'PARTICIPATION'))
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS season_reward_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                season_id INTEGER NOT NULL,
                division_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                reward_id TEXT NOT NULL,
                reward_type TEXT NOT NULL,
                coins_awarded INTEGER NOT NULL DEFAULT 0,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                badge_awarded TEXT DEFAULT NULL,
                status TEXT NOT NULL DEFAULT 'PENDING' CHECK(status IN ('PENDING', 'DISTRIBUTED')),
                distributed_at TIMESTAMP DEFAULT NULL,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(user_id, season_id, reward_id)
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_reward_ledger_user ON season_reward_ledger(user_id, season_id)")

        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES ('010_phase10_economy_and_progression', 'Phase 10: Logovo Economy, Ranking & Seasonal Progression')
        """)

        # Расписание, сгенерированное до фикса, легло с пустыми player1_team /
        # player2_team — матч читается по имени клуба, и без него он безымянный.
        # Проставляем клуб по player1_id один раз; строки с уже заполненной
        # колонкой не трогаем, свободные матчи (player_id IS NULL) — тоже.
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '013_backfill_match_team_names'")
        if not cursor.fetchone():
            for side in ("player1", "player2"):
                # Имена колонок подставляются из литералов цикла, не из данных.
                cursor.execute(f"""
                    UPDATE matches SET {side}_team = (
                        SELECT u.team_name FROM users u WHERE u.telegram_id = matches.{side}_id
                    )
                    WHERE ({side}_team IS NULL OR TRIM({side}_team) = '')
                      AND {side}_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM users u
                          WHERE u.telegram_id = matches.{side}_id
                            AND u.team_name IS NOT NULL AND TRIM(u.team_name) != ''
                      )
                """)
                if cursor.rowcount:
                    logger.info(f"Backfilled {cursor.rowcount} matches.{side}_team from {side}_id.")
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('013_backfill_match_team_names', 'Fill matches.playerN_team from playerN_id for schedules generated without it')
            """)

        # ─── Integrity Engine: детектор договорных матчей ───────────────────
        # Единица анализа — нога ставки (bet_items), а не купон целиком: в
        # экспрессе из пяти матчей договорным может быть ровно один.
        # online_score считается сразу после размещения, post_score — после
        # подтверждения счёта; обе половины живут в одной строке (stage).
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS integrity_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bet_id INTEGER NOT NULL,
                bet_item_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                match_id INTEGER,
                division_id INTEGER,
                season_id INTEGER,
                online_score REAL NOT NULL DEFAULT 0,
                post_score REAL NOT NULL DEFAULT 0,
                total_score REAL NOT NULL DEFAULT 0,
                severity TEXT NOT NULL DEFAULT 'low' CHECK(severity IN ('low','medium','high','critical')),
                stage TEXT NOT NULL DEFAULT 'online' CHECK(stage IN ('online','resolved')),
                low_confidence INTEGER NOT NULL DEFAULT 0,
                features TEXT,
                status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged','dismissed','confirmed')),
                reviewed_by INTEGER,
                reviewed_at TIMESTAMP,
                note TEXT,
                created_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                updated_at TIMESTAMP DEFAULT (datetime('now', '+3 hours')),
                UNIQUE(bet_id, bet_item_id),
                FOREIGN KEY(bet_id) REFERENCES user_bets(id) ON DELETE CASCADE
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_integrity_open ON integrity_cases(status, total_score DESC)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_integrity_match ON integrity_cases(match_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_integrity_user ON integrity_cases(user_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_integrity_stage ON integrity_cases(stage, match_id)")

        # Маркер применённого Elo. Админ может исправить счёт уже подтверждённого
        # матча — без дельт рейтинг применился бы второй раз поверх первого.
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS elo_applied_matches (
                match_id INTEGER PRIMARY KEY,
                team1 TEXT,
                team2 TEXT,
                delta1 REAL NOT NULL DEFAULT 0,
                delta2 REAL NOT NULL DEFAULT 0,
                applied_at TIMESTAMP DEFAULT (datetime('now', '+3 hours'))
            )
        """)

        cursor.execute("""
            INSERT OR IGNORE INTO schema_migrations (version, description)
            VALUES ('017_integrity_engine', 'Match-fixing detector: integrity_cases + elo_applied_matches')
        """)

        # Standardize and migrate canonical team names across all tables
        migrate_team_names_canonical(cursor)

        # Safe migration for squad_players.position
        cursor.execute("PRAGMA table_info(squad_players)")
        squad_cols = [c[1] for c in cursor.fetchall()]
        if "position" not in squad_cols:
            cursor.execute("ALTER TABLE squad_players ADD COLUMN position TEXT")
            logger.info("Migrated squad_players table: added 'position' column.")

        # Safe migration for squad_players.norm_name and norm_team_name
        if "norm_name" not in squad_cols:
            cursor.execute("ALTER TABLE squad_players ADD COLUMN norm_name TEXT")
            logger.info("Migrated squad_players table: added 'norm_name' column.")
        if "norm_team_name" not in squad_cols:
            cursor.execute("ALTER TABLE squad_players ADD COLUMN norm_team_name TEXT")
            logger.info("Migrated squad_players table: added 'norm_team_name' column.")

        # The index goes first: a stored norm_name is recomputed below whenever the
        # normalizer has changed since it was written (e.g. once 'ı' began folding to
        # 'i'), and two rows of one club can land on the same key until the dedup
        # below merges them.
        cursor.execute("DROP INDEX IF EXISTS idx_squad_players_team_norm")

        cursor.execute("SELECT id, team_name, player_name, norm_name, norm_team_name FROM squad_players")
        for p_row in cursor.fetchall():
            p_key = normalize_player_name_key(p_row["player_name"])
            t_key = p_row["norm_team_name"] or normalize_team_name(
                resolve_team_name(p_row["team_name"]) or p_row["team_name"]
            )
            if p_key != p_row["norm_name"] or t_key != p_row["norm_team_name"]:
                cursor.execute(
                    "UPDATE squad_players SET norm_name = ?, norm_team_name = ? WHERE id = ?",
                    (p_key, t_key, p_row["id"])
                )

        # Deduplicate existing duplicate entries within each club
        _deduplicate_squad_players_in_db(cursor)

        # Unique index on (norm_team_name, norm_name) physically prevents duplicate players in same club
        cursor.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_squad_players_team_norm "
            "ON squad_players(norm_team_name, norm_name)"
        )

        # ─── 018: старые строки писались по UTC — переводим на московское время ─
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '018_utc_to_msk_timestamps'")
        if not cursor.fetchone():
            shifted = _shift_timestamps_to_msk(cursor)
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('018_utc_to_msk_timestamps', 'Machine-written timestamps shifted UTC -> MSK (+3h)')
            """)
            if shifted:
                logger.info("Migration 018: shifted %s timestamp values from UTC to MSK", shifted)

        # ─── 019: явный статус тура ─────────────────────────────────────────
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '019_round_status'")
        if not cursor.fetchone():
            cursor.execute("""
                UPDATE rounds SET status = CASE
                    WHEN is_open = 1 THEN 'open'
                    WHEN deadline IS NOT NULL AND TRIM(deadline) != '' THEN 'closed'
                    ELSE 'scheduled'
                END
                WHERE status IS NULL
            """)
            cursor.execute(
                "SELECT division_id, round_number FROM rounds "
                "WHERE is_open = 1 AND (deadline IS NULL OR TRIM(deadline) = '')"
            )
            no_deadline = [(r["division_id"], r["round_number"]) for r in cursor.fetchall()]
            if no_deadline:
                logger.warning(
                    "Migration 019: open rounds without a deadline never become debts, "
                    "set a deadline for them: %s", no_deadline
                )
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('019_round_status', 'rounds.status/closed_at/closed_by: explicit round lifecycle')
            """)

        # ─── 020: долги — отдельная таблица вместо флагов debt_reminders ───
        cursor.execute("SELECT 1 FROM schema_migrations WHERE version = '020_match_debts'")
        if not cursor.fetchone():
            migrated = _snapshot_legacy_debts(cursor, now_msk())
            cursor.execute("""
                INSERT OR IGNORE INTO schema_migrations (version, description)
                VALUES ('020_match_debts', 'match_debts: one row per debt, stages moved from debt_reminders')
            """)
            if migrated:
                logger.info("Migration 020: %s debts moved to match_debts", migrated)

        # ─── 021: predictions — одна строка на (match_id, model_version) ───────
        _ensure_prediction_uniqueness(cursor)

        # ─── 022: общий кубок — стадии, серии, поля матча ─────────────────────
        _ensure_cup_schema(cursor)

        # ─── 024: запрет ставок для отдельного игрока (панель Logovo.bet) ─────
        _ensure_betting_bans(cursor)

        # ─── 025: ежедневный бонус убран — его настройка из панели не читается ─
        _drop_daily_bonus_setting(cursor)

        # ─── 026: журнал «ИИ-прогноза» для сверки с сыгранными матчами ────────
        _ensure_ai_pick_log(cursor)

        # ─── 027: кубки дивизионов — cup_stages.division_id, ключ серий ───────
        _ensure_division_cups(cursor)

        # Seed initial catalog data
        seed_gamification_catalog(cursor)

        logger.info("Database tables initialized successfully.")


def get_cached_telegram_media(file_hash: str, media_type: str = "animation") -> str | None:
    """Retrieve cached Telegram file_id by media SHA-256 hash."""
    if not file_hash:
        return None
    try:
        with transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT file_id FROM telegram_media_cache WHERE file_hash = ? AND media_type = ?",
                (file_hash, media_type)
            )
            row = cursor.fetchone()
            return row["file_id"] if row else None
    except Exception as e:
        logger.warning(f"Error fetching cached telegram media for hash {file_hash[:8]}: {e}")
        return None


def save_cached_telegram_media(file_hash: str, file_id: str, media_type: str = "animation") -> None:
    """Save or update Telegram file_id for given media SHA-256 hash."""
    if not file_hash or not file_id:
        return
    try:
        with transaction() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO telegram_media_cache (file_hash, file_id, media_type, created_at)
                VALUES (?, ?, ?, datetime('now', '+3 hours'))
                ON CONFLICT(file_hash) DO UPDATE SET file_id = excluded.file_id, media_type = excluded.media_type
                """,
                (file_hash, file_id, media_type)
            )
    except Exception as e:
        logger.warning(f"Error saving cached telegram media for hash {file_hash[:8]}: {e}")


def clear_telegram_media_cache(media_type: str | None = None) -> int:
    """Clear cached Telegram file_ids from telegram_media_cache."""
    try:
        with transaction() as conn:
            cursor = conn.cursor()
            if media_type:
                cursor.execute("SELECT COUNT(*) FROM telegram_media_cache WHERE media_type = ?", (media_type,))
                cnt = cursor.fetchone()[0]
                cursor.execute("DELETE FROM telegram_media_cache WHERE media_type = ?", (media_type,))
            else:
                cursor.execute("SELECT COUNT(*) FROM telegram_media_cache")
                cnt = cursor.fetchone()[0]
                cursor.execute("DELETE FROM telegram_media_cache")
            return cnt
    except Exception as e:
        logger.warning(f"Error clearing telegram media cache: {e}")
        return 0


def migrate_team_names_canonical(cursor: sqlite3.Cursor) -> None:
    """Migrate and standardize legacy/variant team spellings across all DB tables."""
    try:
        # Standardize 'Будё Глимт' (latin ë \u00eb, 'Буде Глимт', 'Буде-Глимт', etc.)
        for tbl, cols in [
            ("users", ["team_name"]),
            ("matches", ["player1_team", "player2_team"]),
            ("squad_players", ["team_name"]),
            ("match_events", ["team_name"]),
            ("cup_series", ["team1_name", "team2_name", "winner_name"])
        ]:
            for col in cols:
                cursor.execute(f"""
                    UPDATE {tbl} 
                    SET {col} = 'Будё Глимт' 
                    WHERE {col} IS NOT NULL AND (
                        {col} LIKE '%буд%глимт%' OR {col} LIKE '%bodo%glimt%' OR {col} = 'Буде Глимт' OR {col} = 'Будë Глимт'
                    ) AND {col} != 'Будё Глимт'
                """)
                cursor.execute(f"""
                    UPDATE {tbl} 
                    SET {col} = 'Порту' 
                    WHERE {col} IS NOT NULL AND LOWER({col}) IN ('порто', 'porto', 'portu') AND {col} != 'Порту'
                """)
                cursor.execute(f"""
                    UPDATE {tbl} 
                    SET {col} = 'Фейеноорд' 
                    WHERE {col} IS NOT NULL AND LOWER({col}) IN ('фейенорд', 'фейноорд', 'фейнорд', 'feyenoord') AND {col} != 'Фейеноорд'
                """)
    except Exception as e:
        logger.warning(f"migrate_team_names_canonical notice: {e}")

# Резолв имени клуба переехал в club_registry.py (аудит P3-7): это чистый CPU без SQL.
# Имена ниже реэкспортируются, поэтому database.resolve_team_name(...) и
# from database import normalize_team_name продолжают работать без изменений.
import club_registry
from club_registry import (  # noqa: F401  (re-export)
    TEAM_ALIASES,
    normalize_team_name,
    resolve_team_name,
    teams_match,
)

def verify_registry_against_db(division_id: int | None = None) -> dict[str, list[str]]:
    """Сверить config.CLUB_REGISTRY с клубами, которые реально заведены в users.

    Реестр правится руками, а клубы заводят тренеры — списки расходятся молча.
    Само по себе расхождение резолв не ломает: клуб вне реестра резолвится сам в
    себя. Но teams_match для него становится строже — опечатку OCR не с чем
    сличить, — поэтому дрейф надо видеть, а не узнавать о нём из жалобы.

    Возвращает два отсортированных списка:
      missing_in_registry — клубы из БД, которых нет в реестре: их надо добавить;
      unused_in_registry  — имена реестра, под которыми никто не играет: опечатка
                            в реестре либо ушедший клуб.

    division_id сужает проверку до одного дивизиона; None — весь турнир.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is None:
            cursor.execute(
                "SELECT DISTINCT team_name FROM users "
                "WHERE team_name IS NOT NULL AND team_name != ''"
            )
        else:
            cursor.execute(
                "SELECT DISTINCT team_name FROM users "
                "WHERE team_name IS NOT NULL AND team_name != '' AND division_id = ?",
                (division_id,)
            )
        db_names = [row["team_name"] for row in cursor.fetchall()]

    registry_index = club_registry.get_registry_index()
    db_index = {normalize_team_name(name): name for name in db_names}

    missing = sorted(raw for norm, raw in db_index.items() if norm not in registry_index)
    # Ушедшие клубы ищем только по всему турниру: в срезе одного дивизиона
    # «неиспользованным» окажется весь остальной реестр.
    unused = (
        sorted(raw for norm, raw in registry_index.items() if norm not in db_index)
        if division_id is None else []
    )
    return {"missing_in_registry": missing, "unused_in_registry": unused}

def get_team_owner(team_name: str) -> int | None:
    """Return the telegram_id of the user who owns the given team."""
    if not team_name:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, team_name FROM users WHERE team_name IS NOT NULL")
        rows = cursor.fetchall()
        for r in rows:
            if teams_match(r['team_name'], team_name):
                return r['telegram_id']
        return None


def find_coach_by_club(club_query: str, division_id: int | None = None) -> dict | None:
    """
    Find a coach by club name or alias.
    If division_id is specified, searches that division first; otherwise searches all coaches.
    Returns dict with telegram_id, username, team_name, division_id, or None.
    """
    if not club_query:
        return None
    target = club_query.strip()
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, username, team_name, division_id FROM users WHERE team_name IS NOT NULL"
        )
        rows = cursor.fetchall()
        matches = []
        for r in rows:
            if teams_match(r["team_name"], target):
                matches.append(dict(r))

        if not matches:
            return None

        # Prioritize division if provided
        if division_id is not None:
            div_matches = [m for m in matches if m.get("division_id") == division_id]
            if div_matches:
                return div_matches[0]

        return matches[0]


def get_coaches_for_division(division_id: int | None = None) -> list[dict]:
    """
    Retrieve all users who have an assigned team_name, optionally filtered by division_id.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute(
                "SELECT telegram_id, username, team_name, division_id FROM users WHERE team_name IS NOT NULL AND division_id = ? ORDER BY team_name ASC",
                (division_id,)
            )
        else:
            cursor.execute(
                "SELECT telegram_id, username, team_name, division_id FROM users WHERE team_name IS NOT NULL ORDER BY team_name ASC"
            )
        return [dict(r) for r in cursor.fetchall()]

def get_user(telegram_id: int) -> sqlite3.Row | None:
    """Retrieve a user record by Telegram ID."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_id, username, team_name, league_name, role, registered_at, squad_photo_id, warn_count, pending_notification, division_id FROM users WHERE telegram_id = ?
        """, (telegram_id,))
        return cursor.fetchone()

def register_user(telegram_id: int, username: str | None, role: str = 'player', team_name: str | None = None, league_name: str | None = None) -> None:
    """Create or update user profile with team and league assignment."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE users SET username = ?, role = ?, team_name = COALESCE(?, team_name), league_name = COALESCE(?, league_name) WHERE telegram_id = ?",
                (username, role, team_name, league_name, telegram_id)
            )
        else:
            cursor.execute(
                "INSERT INTO users (telegram_id, username, role, team_name, league_name, registered_at) VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))",
                (telegram_id, username, role, team_name, league_name)
            )


def upsert_user(telegram_id: int, username: str | None, role: str = 'user') -> None:
    """Create a new user or update their username and role if they exist."""
    with transaction() as conn:
        cursor = conn.cursor()
        # Find if user already exists
        cursor.execute("SELECT role FROM users WHERE telegram_id = ?", (telegram_id,))
        exists = cursor.fetchone()
        if exists:
            # Update username and role, preserve team_name, league_name
            cursor.execute(
                "UPDATE users SET username = ?, role = ? WHERE telegram_id = ?",
                (username, role, telegram_id)
            )
        else:
            cursor.execute(
                "INSERT INTO users (telegram_id, username, role, registered_at) VALUES (?, ?, ?, datetime('now', '+3 hours'))",
                (telegram_id, username, role)
            )

def update_profile(telegram_id: int, team_name: str, league_name: str) -> None:
    """Update game profile details for a registered user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET team_name = ?, league_name = ? WHERE telegram_id = ?",
            (team_name, league_name, telegram_id)
        )

def list_users() -> list[sqlite3.Row]:
    """Retrieve all registered users."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, username, team_name, league_name, role, division_id, registered_at, COALESCE(warn_count, 0) AS warn_count FROM users ORDER BY registered_at DESC"
        )
        return cursor.fetchall()

def get_player_stats(telegram_id: int) -> dict:
    """Calculate and return match statistics for a player using a single aggregated SQL query."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (telegram_id,))
        u_row = cursor.fetchone()
        u_team = u_row["team_name"] if u_row and u_row["team_name"] else ""

        cursor.execute("""
            SELECT
                SUM(CASE 
                    WHEN (player1_team = ? AND player1_score > player2_score) OR (player2_team = ? AND player2_score > player1_score) THEN 1 
                    ELSE 0 
                END) AS wins,
                SUM(CASE 
                    WHEN player1_score = player2_score THEN 1 
                    ELSE 0 
                END) AS draws,
                SUM(CASE 
                    WHEN (player1_team = ? AND player1_score < player2_score) OR (player2_team = ? AND player2_score < player1_score) THEN 1 
                    ELSE 0 
                END) AS losses,
                SUM(CASE 
                    WHEN player1_team = ? THEN COALESCE(player1_score, 0)
                    WHEN player2_team = ? THEN COALESCE(player2_score, 0)
                    ELSE 0 
                END) AS goals_scored,
                SUM(CASE 
                    WHEN player1_team = ? THEN COALESCE(player2_score, 0)
                    WHEN player2_team = ? THEN COALESCE(player1_score, 0)
                    ELSE 0 
                END) AS goals_conceded
            FROM matches
            WHERE status = 'confirmed' AND (player1_team = ? OR player2_team = ?)
        """, (u_team, u_team, u_team, u_team, u_team, u_team, u_team, u_team, u_team, u_team))
        
        row = cursor.fetchone()
        wins = row["wins"] or 0 if row else 0
        draws = row["draws"] or 0 if row else 0
        losses = row["losses"] or 0 if row else 0
        goals_scored = row["goals_scored"] or 0 if row else 0
        goals_conceded = row["goals_conceded"] or 0 if row else 0
        
        played = wins + draws + losses
        points = wins * 3 + draws
        
        return {
            "played": played,
            "wins": wins,
            "draws": draws,
            "losses": losses,
            "goals_scored": goals_scored,
            "goals_conceded": goals_conceded,
            "points": points
        }

def get_user_tournament_summary(telegram_id: int, season_id: int | None = None) -> dict:
    """Tournament (не беттинговый) профиль участника для кабинета Mini App.

    Возвращает место в таблице своего дивизиона, очки и матчевую статистику,
    а также форму по последним пяти сыгранным матчам. Если пользователь не
    привязан к команде/дивизиону — `registered` = False и остальные поля пустые.
    """
    user = get_user(telegram_id)
    team_name = (user["team_name"] if user and user["team_name"] else "") or ""
    div_id = (user["division_id"] if user and "division_id" in user.keys() else None)

    if not team_name:
        return {
            "registered": False,
            "team_name": None,
            "division_id": div_id,
            "division_name": None,
            "position": None,
            "total_teams": 0,
            "played": 0, "wins": 0, "draws": 0, "losses": 0,
            "goals_scored": 0, "goals_conceded": 0, "goal_diff": 0,
            "points": 0, "form": [],
        }

    division = get_division(div_id) if div_id is not None else None
    standings = get_standings(division_id=div_id, season_id=season_id)

    position = None
    row = None
    for idx, entry in enumerate(standings, start=1):
        if entry.get("telegram_id") == telegram_id:
            position, row = idx, entry
            break

    # Таблица считается только по подтверждённым матчам дивизиона; если строки
    # нет (например, игрок вне активного дивизиона), падаем на общий подсчёт.
    stats = row if row else get_player_stats(telegram_id)

    with transaction() as conn:
        cursor = conn.cursor()
        params: list = [team_name, team_name]
        query = """
            SELECT player1_team, player2_team, player1_score, player2_score
            FROM matches
            WHERE status = 'confirmed'
              AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?))
        """
        if div_id is not None:
            query += " AND (division_id = ? OR division_id IS NULL)"
            params.append(div_id)
        query += " ORDER BY id DESC LIMIT 5"
        cursor.execute(query, params)
        form = []
        for m in cursor.fetchall():
            is_home = (m["player1_team"] or "").lower() == team_name.lower()
            own = m["player1_score"] if is_home else m["player2_score"]
            opp = m["player2_score"] if is_home else m["player1_score"]
            if own is None or opp is None:
                continue
            form.append("W" if own > opp else ("D" if own == opp else "L"))

    scored = int(stats.get("goals_scored") or 0)
    conceded = int(stats.get("goals_conceded") or 0)
    return {
        "registered": True,
        "team_name": team_name,
        "division_id": div_id,
        "division_name": (division or {}).get("name") if division else None,
        "position": position,
        "total_teams": len(standings),
        "played": int(stats.get("played") or 0),
        "wins": int(stats.get("wins") or 0),
        "draws": int(stats.get("draws") or 0),
        "losses": int(stats.get("losses") or 0),
        "goals_scored": scored,
        "goals_conceded": conceded,
        "goal_diff": scored - conceded,
        "points": int(stats.get("points") or 0),
        "form": form,
    }


def get_active_matches(telegram_id: int, only_expired_deadlines: bool = False, division_id: int | None = None) -> list[dict]:
    """Retrieve active matches for a user from Round 1 up to the highest OPEN round number in their division."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name, division_id FROM users WHERE telegram_id = ?", (telegram_id,))
        u_row = cursor.fetchone()
        u_team = u_row["team_name"] if u_row and u_row["team_name"] else ""
        user_div_id = division_id if division_id is not None else (u_row["division_id"] if u_row and "division_id" in u_row.keys() else None)

        # Get highest open round number for user's division (or global if unassigned)
        if user_div_id is not None:
            cursor.execute("SELECT MAX(round_number) FROM rounds WHERE is_open = 1 AND division_id = ?", (user_div_id,))
        else:
            cursor.execute("SELECT MAX(round_number) FROM rounds WHERE is_open = 1")
        max_open_row = cursor.fetchone()
        max_open_round = max_open_row[0] if max_open_row and max_open_row[0] is not None else 0

        # Collect round numbers whose deadline has already passed (for debt filtering)
        expired_rounds: set[int] = set()
        if only_expired_deadlines:
            now = now_msk()
            if user_div_id is not None:
                cursor.execute("SELECT round_number, deadline FROM rounds WHERE is_open = 1 AND division_id = ?", (user_div_id,))
            else:
                cursor.execute("SELECT round_number, deadline FROM rounds WHERE is_open = 1")
            for r_num, dl_str in cursor.fetchall():
                if not dl_str:
                    continue
                dl_dt = parse_flexible_datetime(dl_str)
                if dl_dt and dl_dt <= now:
                    expired_rounds.add(r_num)

        div_filter = f"AND (m.division_id = {user_div_id} OR m.division_id IS NULL) " if user_div_id is not None else ""
        league_condition = (
            f"(m.tournament_type IS NULL OR m.tournament_type = 'league') "
            f"{div_filter}"
            f"AND m.round_number IN ({','.join('?' * len(expired_rounds))}) "
            "AND m.status IN ('pending', 'reported', 'disputed')"
            if only_expired_deadlines and expired_rounds
            else (
                f"(m.tournament_type IS NULL OR m.tournament_type = 'league') "
                f"{div_filter}"
                "AND m.round_number >= 1 "
                "AND m.round_number <= ? "
                "AND m.status IN ('pending', 'reported', 'disputed')"
            )
        )

        params: list = [u_team, u_team, u_team, u_team]
        if only_expired_deadlines and expired_rounds:
            params.extend(sorted(expired_rounds))
        else:
            params.append(max_open_round)

        cursor.execute(f"""
            SELECT 
                m.id, m.round_number, m.status, m.tournament_type, m.cup_stage, m.cup_series_id, m.game_num_in_series,
                m.player1_team, m.player2_team, u1.telegram_id AS player1_id, u2.telegram_id AS player2_id,
                u1.username AS p1_username, u1.team_name AS p1_team,
                u2.username AS p2_username, u2.team_name AS p2_team
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE (
                (LOWER(m.player1_team) = LOWER(?) AND ? != '')
                OR (LOWER(m.player2_team) = LOWER(?) AND ? != '')
            )
            AND (
                (m.tournament_type = 'cup' AND m.status IN ('pending', 'reported', 'disputed'))
                OR
                {league_condition}
            )
            ORDER BY m.round_number ASC, m.id ASC
        """, params)
        
        matches = []
        for row in cursor.fetchall():
            d = dict(row)
            
            # Skip cup matches where one of the teams is still a placeholder
            if d['tournament_type'] == 'cup':
                if (d['player1_team'] and d['player1_team'].startswith("Победитель")) or \
                   (d['player2_team'] and d['player2_team'].startswith("Победитель")):
                    continue
                    
            if u_team and d['player1_team'] and d['player1_team'].lower() == u_team.lower():
                d['opponent_team'] = d['player2_team'] or d['p2_team']
                d['opponent_username'] = d['p2_username']
            else:
                d['opponent_team'] = d['player1_team'] or d['p1_team']
                d['opponent_username'] = d['p1_username']
            matches.append(d)
        return matches

get_pending_matches = get_active_matches

def get_match_history(telegram_id: int) -> list[dict]:
    """Retrieve played (confirmed) matches for a user, including opponent profile details."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (telegram_id,))
        u_row = cursor.fetchone()
        u_team = u_row["team_name"] if u_row and u_row["team_name"] else ""
        if not u_team:
            return []

        cursor.execute("""
            SELECT 
                m.id, m.round_number, m.player1_score, m.player2_score,
                u1.telegram_id AS player1_id, u2.telegram_id AS player2_id,
                m.player1_team, m.player2_team,
                o.telegram_id AS opponent_id,
                o.username AS opponent_username,
                COALESCE(o.team_name, CASE WHEN LOWER(m.player1_team) = LOWER(?) THEN m.player2_team ELSE m.player1_team END) AS opponent_team
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            LEFT JOIN users o ON (
                (LOWER(m.player1_team) = LOWER(?) AND LOWER(m.player2_team) = LOWER(o.team_name)) OR
                (LOWER(m.player2_team) = LOWER(?) AND LOWER(m.player1_team) = LOWER(o.team_name))
            )
            WHERE (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?)) AND m.status = 'confirmed'
            ORDER BY m.played_at DESC, m.round_number DESC
        """, (u_team, u_team, u_team, u_team, u_team))
        return [dict(row) for row in cursor.fetchall()]

def update_single_field(telegram_id: int, field_name: str, value: str) -> None:
    """Update a single specific field for a user profile safely."""
    if field_name not in ("team_name", "league_name", "squad_photo_id"):
        raise ValueError(f"Invalid field name: {field_name}")
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE users SET {field_name} = ? WHERE telegram_id = ?",
            (value, telegram_id)
        )

def log_admin_action(
    admin_id: int,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    old_value: str | None = None,
    new_value: str | None = None,
    reason: str | None = None,
    division_id: int | None = None,
    season_id: int | None = None,
    metadata: str | None = None
) -> None:
    """Record an administrative action into admin_audit_log."""
    try:
        with transaction() as conn:
            conn.cursor().execute(
                """
                INSERT INTO admin_audit_log (
                    admin_id, action, target_type, target_id, old_value, new_value, division_id, season_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
                """,
                (admin_id, action, target_type, target_id, old_value, new_value or reason or metadata, division_id, season_id)
            )
    except Exception as e:
        logger.warning(f"Failed to log admin action '{action}': {e}")


def create_season(name: str, created_by: int | None = None) -> int:
    """Create a new season in draft status."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO seasons (name, status, created_by, created_at) VALUES (?, 'draft', ?, datetime('now', '+3 hours'))",
            (name.strip(), created_by)
        )
        season_id = cursor.lastrowid
        log_admin_action(
            admin_id=created_by or 0,
            action="create_season",
            target_type="season",
            target_id=season_id,
            new_value=name.strip(),
            season_id=season_id
        )
        return season_id


def get_season(season_id: int) -> dict | None:
    """Retrieve a season by ID."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seasons WHERE id = ?", (season_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


class SeasonInt(int):
    """Integer season_id that supports dict-like subscripting and methods for backwards compatibility."""
    def __new__(cls, val, data=None):
        inst = super().__new__(cls, int(val))
        inst._data = data or {}
        return inst

    def __getitem__(self, item):
        return self._data[item]

    def get(self, item, default=None):
        return self._data.get(item, default)

    def __contains__(self, item):
        return item in self._data

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        return self._data.values()

    def items(self):
        return self._data.items()


def get_active_season() -> int | None:
    """Retrieve the currently active season ID (returns SeasonInt which acts as both int and dict)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seasons WHERE status = 'active' AND name NOT LIKE '%TEST%' AND name NOT LIKE '%LAB%' ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        if row:
            return SeasonInt(row["id"], dict(row))
        cursor.execute("SELECT * FROM seasons WHERE name NOT LIKE '%TEST%' AND name NOT LIKE '%LAB%' ORDER BY id DESC LIMIT 1")
        row = cursor.fetchone()
        return SeasonInt(row["id"], dict(row)) if row else None


def get_active_divisions(season_id: int | None = None) -> list[dict]:
    """Retrieve active divisions for the season (list of dicts [{'id': 1, 'name': 'Дивизион 1'}, ...])."""
    ensure_canonical_divisions()
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is not None:
            cursor.execute(
                "SELECT id, name, code, sort_order FROM divisions WHERE is_active = 1 AND (season_id = ? OR season_id IS NULL) ORDER BY sort_order ASC, id ASC",
                (int(season_id),)
            )
            rows = cursor.fetchall()
            if rows:
                return [{"id": r["id"], "name": r["name"], "code": r["code"]} for r in rows]
        cursor.execute(
            "SELECT id, name, code, sort_order FROM divisions WHERE is_active = 1 ORDER BY sort_order ASC, id ASC"
        )
        return [{"id": r["id"], "name": r["name"], "code": r["code"]} for r in cursor.fetchall()]


async def async_get_active_season() -> int | None:
    """Asynchronous wrapper for get_active_season."""
    return await asyncio.to_thread(get_active_season)


async def async_get_active_divisions(season_id: int) -> list[dict]:
    """Asynchronous wrapper for get_active_divisions."""
    return await asyncio.to_thread(get_active_divisions, season_id)


def list_seasons(status: str | None = None) -> list[dict]:
    """List all seasons, optionally filtered by status."""
    with transaction() as conn:
        cursor = conn.cursor()
        if status:
            cursor.execute("SELECT * FROM seasons WHERE status = ? ORDER BY id DESC", (status,))
        else:
            cursor.execute("SELECT * FROM seasons ORDER BY id DESC")
        return [dict(r) for r in cursor.fetchall()]


def activate_season(season_id: int, actor_user_id: int | None = None) -> tuple[bool, str]:
    """
    Transition a season to 'active'.
    Marks previous active season as 'finished'.
    """
    now = now_msk_str()
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seasons WHERE id = ?", (season_id,))
        target = cursor.fetchone()
        if not target:
            return False, f"Сезон #{season_id} не найден."
        if target["status"] == "active":
            return True, f"Сезон #{season_id} уже активен."
        if target["status"] in ("finished", "archived"):
            return False, f"Нельзя напрямую активировать завершённый или архивированный сезон #{season_id}."

        cursor.execute(
            "UPDATE seasons SET status = 'finished', finished_at = ? WHERE status = 'active'",
            (now,)
        )
        cursor.execute(
            "UPDATE seasons SET status = 'active', started_at = ? WHERE id = ?",
            (now, season_id)
        )
        log_admin_action(
            admin_id=actor_user_id or 0,
            action="activate_season",
            target_type="season",
            target_id=season_id,
            new_value="active",
            season_id=season_id
        )
        return True, f"Сезон #{season_id} ('{target['name']}') успешно активирован."


def finish_season(season_id: int, actor_user_id: int | None = None) -> tuple[bool, str]:
    """Transition an active season to 'finished'."""
    now = now_msk_str()
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seasons WHERE id = ?", (season_id,))
        target = cursor.fetchone()
        if not target:
            return False, f"Сезон #{season_id} не найден."
        if target["status"] != "active":
            return False, f"Завершить можно только активный сезон (текущий статус: {target['status']})."

        cursor.execute(
            "UPDATE seasons SET status = 'finished', finished_at = ? WHERE id = ?",
            (now, season_id)
        )
        log_admin_action(
            admin_id=actor_user_id or 0,
            action="finish_season",
            target_type="season",
            target_id=season_id,
            new_value="finished",
            season_id=season_id
        )
        return True, f"Сезон #{season_id} успешно завершён."


def archive_season(season_id: int, actor_user_id: int | None = None) -> tuple[bool, str]:
    """Transition a finished season to 'archived'."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM seasons WHERE id = ?", (season_id,))
        target = cursor.fetchone()
        if not target:
            return False, f"Сезон #{season_id} не найден."
        if target["status"] != "finished":
            return False, f"Архивировать можно только завершённый сезон (текущий статус: {target['status']})."

        cursor.execute("UPDATE seasons SET status = 'archived' WHERE id = ?", (season_id,))
        log_admin_action(
            admin_id=actor_user_id or 0,
            action="archive_season",
            target_type="season",
            target_id=season_id,
            new_value="archived",
            season_id=season_id
        )
        return True, f"Сезон #{season_id} перенесён в архив."


def get_standings(division_id: int | None = None, season_id: int | None = None, up_to_round: int | None = None) -> list[dict]:
    """Calculate the standings of registered players dynamically, strictly scoped by division and season.

    up_to_round caps the table at that round inclusive, so callers can compare
    "before" and "after" snapshots (used by the round digest to draw movement
    arrows). None keeps the full-season behaviour.
    """
    with transaction() as conn:
        cursor = conn.cursor()

        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        teams = {}
        if division_id is not None:
            # Users assigned to this specific division
            cursor.execute(
                "SELECT telegram_id, team_name, username FROM users WHERE division_id = ? AND team_name IS NOT NULL AND team_name != ''",
                (division_id,)
            )
            div_users = cursor.fetchall()
            for row in div_users:
                canon = resolve_team_name(row["team_name"]) or row["team_name"]
                teams[canon] = {
                    "telegram_id": row["telegram_id"],
                    "team_name": canon,
                    "username": row["username"] if row["username"] else "",
                    "played": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "goals_scored": 0,
                    "goals_conceded": 0,
                    "points": 0,
                }
            
            # Fetch confirmed league matches strictly for this division & season
            cursor.execute("""
                SELECT 
                    COALESCE(m.player1_team, u1.team_name) AS player1_team, 
                    COALESCE(m.player2_team, u2.team_name) AS player2_team, 
                    m.player1_score, 
                    m.player2_score 
                FROM matches m
                LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
                LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
                WHERE m.status = 'confirmed'
                  AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
                  AND m.division_id = ?
                  AND (m.season_id = ? OR m.season_id IS NULL)
                  AND (? IS NULL OR m.round_number <= ?)
            """, (division_id, target_season_id, up_to_round, up_to_round))
        else:
            cursor.execute("SELECT telegram_id, team_name, username FROM users WHERE team_name IS NOT NULL AND team_name != ''")
            all_users = cursor.fetchall()
            for row in all_users:
                canon = resolve_team_name(row["team_name"]) or row["team_name"]
                teams[canon] = {
                    "telegram_id": row["telegram_id"],
                    "team_name": canon,
                    "username": row["username"] if row["username"] else "",
                    "played": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "goals_scored": 0,
                    "goals_conceded": 0,
                    "points": 0,
                }

            # Get all confirmed matches for this season
            cursor.execute("""
                SELECT 
                    COALESCE(m.player1_team, u1.team_name) AS player1_team, 
                    COALESCE(m.player2_team, u2.team_name) AS player2_team, 
                    m.player1_score, 
                    m.player2_score 
                FROM matches m
                LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
                LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
                WHERE m.status = 'confirmed'
                  AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
                  AND (m.season_id = ? OR m.season_id IS NULL)
                  AND (m.division_id = 1 OR m.division_id IS NULL)
                  AND (? IS NULL OR m.round_number <= ?)
            """, (target_season_id, up_to_round, up_to_round))
        
        matches = cursor.fetchall()

        for match in matches:
            raw_t1 = match["player1_team"] or ""
            raw_t2 = match["player2_team"] or ""
            t1 = resolve_team_name(raw_t1) or raw_t1
            t2 = resolve_team_name(raw_t2) or raw_t2
            p1_score = match["player1_score"]
            p2_score = match["player2_score"]

            if p1_score is None or p2_score is None:
                continue

            # Match t1
            matched_u1 = teams.get(t1)
            if not matched_u1:
                for k, obj in teams.items():
                    if teams_match(k, t1):
                        matched_u1 = obj
                        break

            # Match t2
            matched_u2 = teams.get(t2)
            if not matched_u2:
                for k, obj in teams.items():
                    if teams_match(k, t2):
                        matched_u2 = obj
                        break

            # Team played a confirmed match but has no row in users: add it on the fly.
            # Both branches seed `teams` from users only, so a club whose coach has not
            # registered yet would otherwise drop its matches out of the table silently.
            if matched_u1 is None and t1:
                teams[t1] = {
                    "telegram_id": None,
                    "team_name": t1,
                    "username": "",
                    "played": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "goals_scored": 0,
                    "goals_conceded": 0,
                    "points": 0,
                }
                matched_u1 = teams[t1]

            if matched_u2 is None and t2:
                teams[t2] = {
                    "telegram_id": None,
                    "team_name": t2,
                    "username": "",
                    "played": 0,
                    "wins": 0,
                    "draws": 0,
                    "losses": 0,
                    "goals_scored": 0,
                    "goals_conceded": 0,
                    "points": 0,
                }
                matched_u2 = teams[t2]

            if matched_u1:
                matched_u1["played"] += 1
                matched_u1["goals_scored"] += p1_score
                matched_u1["goals_conceded"] += p2_score
                if p1_score > p2_score:
                    matched_u1["wins"] += 1
                    matched_u1["points"] += 3
                elif p1_score < p2_score:
                    matched_u1["losses"] += 1
                else:
                    matched_u1["draws"] += 1
                    matched_u1["points"] += 1

            if matched_u2:
                matched_u2["played"] += 1
                matched_u2["goals_scored"] += p2_score
                matched_u2["goals_conceded"] += p1_score
                if p2_score > p1_score:
                    matched_u2["wins"] += 1
                    matched_u2["points"] += 3
                elif p2_score < p1_score:
                    matched_u2["losses"] += 1
                else:
                    matched_u2["draws"] += 1
                    matched_u2["points"] += 1

        # Convert to list and sort
        standings_list = list(teams.values())
        standings_list.sort(
            key=lambda x: (
                x["points"],
                x["goals_scored"] - x["goals_conceded"],
                x["goals_scored"],
                x["wins"]
            ),
            reverse=True
        )
        return standings_list

def clear_all_matches() -> None:
    """Delete all matches from the matches table."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM matches")

def get_match(match_id: int) -> dict | None:
    """Retrieve a single match by ID with player nicknames, team names, and cup details."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                m.id, m.round_number, COALESCE(u1.telegram_id, m.player1_id) AS player1_id, COALESCE(u2.telegram_id, m.player2_id) AS player2_id,
                m.player1_score, m.player2_score, m.status, m.played_at, m.is_extended,
                COALESCE(m.frozen_seconds, 0) AS frozen_seconds, m.frozen_at,
                m.photo_id, m.dispute_photos, m.reported_by, m.mvp_player,
                m.proposed_time, m.proposed_by, m.time_status,
                m.tournament_type, m.cup_stage, m.cup_series_id, m.game_num_in_series, m.division_id, m.season_id,
                m.cup_winner_team,
                m.player1_team AS direct_p1_team, m.player2_team AS direct_p2_team,
                u1.username AS player1_nickname, u1.team_name AS u1_team, u1.username AS player1_username,
                u2.username AS player2_nickname, u2.team_name AS u2_team, u2.username AS player2_username
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.id = ?
        """, (match_id,))
        row = cursor.fetchone()
        if not row:
            return None
        d = dict(row)
        d['player1_team'] = d['direct_p1_team'] or d['u1_team']
        d['player2_team'] = d['direct_p2_team'] or d['u2_team']
        if not d.get('player1_id') and d.get('player1_team'):
            u1 = find_user_by_team(d['player1_team'])
            if u1:
                d['player1_id'] = u1['telegram_id']
                if not d.get('player1_username'):
                    d['player1_username'] = u1.get('username')
                    d['player1_nickname'] = u1.get('username')
        if not d.get('player2_id') and d.get('player2_team'):
            u2 = find_user_by_team(d['player2_team'])
            if u2:
                d['player2_id'] = u2['telegram_id']
                if not d.get('player2_username'):
                    d['player2_username'] = u2.get('username')
                    d['player2_nickname'] = u2.get('username')
        return d

get_match_by_id = get_match

def confirm_and_finalize_match(match_id: int, p1_score: int, p2_score: int, events: list, reporter_id: int = None, photo_id: str = None, mvp_player: str | None = None) -> str | None:
    """Instantly save and confirm a match with events in database.

    `mvp_player` — игрок матча с золотой короны скриншота (или None). Пустая
    строка нормализуется в NULL, чтобы `get_top_mvps` не считала «безымянных».
    """
    if p1_score < 0 or p2_score < 0:
        raise ValueError("Scores must be non-negative integers")
    with transaction() as conn:
        cursor = conn.cursor()
        # Без этой проверки UPDATE по несуществующему id молча затрагивает 0 строк,
        # транзакция коммитится, вызывающий код считает матч сохранённым — и результат
        # уходит в РЕЗУЛЬТАТЫ, хотя в базе его нет.
        cursor.execute("SELECT id FROM matches WHERE id = ?", (match_id,))
        if not cursor.fetchone():
            raise ValueError(f"Match {match_id} not found: nothing to confirm")
        # Кубковая игра этапа, который ещё не стартовал: линия на неё открыта,
        # и результат до старта — это ставка на известный исход. Проверка здесь,
        # а не только в кнопках: сюда сходятся кабинет, черновики и админка.
        closed = cup_results_closed_reason(match_id)
        if closed:
            raise ValueError(closed)

        cursor.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        aggregated = {}
        for item in events:
            t_name = item[0].strip()
            p_name = item[1].strip()
            e_type = item[2]
            cnt = item[3] if len(item) > 3 else 1

            squad_match = find_player_in_squad(p_name, t_name, conn=conn)
            canon_p_name = squad_match["player_name"] if squad_match else p_name

            key = (t_name, canon_p_name, e_type)
            aggregated[key] = aggregated.get(key, 0) + cnt

        for (t_name, p_name, e_type), cnt in aggregated.items():
            cursor.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, ?, ?)",
                (match_id, t_name, p_name, e_type, cnt)
            )
        mvp_clean = (mvp_player or "").strip() or None
        if mvp_clean:
            cursor.execute("SELECT player1_team, player2_team FROM matches WHERE id = ?", (match_id,))
            m_row = cursor.fetchone()
            if m_row:
                t1, t2 = m_row["player1_team"], m_row["player2_team"]
                m1 = find_player_in_squad(mvp_clean, t1, conn=conn) if t1 else None
                m2 = find_player_in_squad(mvp_clean, t2, conn=conn) if t2 else None
                if m1 and not m2:
                    mvp_clean = m1["player_name"]
                elif m2 and not m1:
                    mvp_clean = m2["player_name"]
        cursor.execute(
            "UPDATE matches SET player1_score = ?, player2_score = ?, reported_by = ?, photo_id = ?, "
            "mvp_player = ?, status = 'confirmed', played_at = ? WHERE id = ?",
            (p1_score, p2_score, reporter_id, photo_id, mvp_clean,
             now_msk_str(), match_id)
        )
        if cursor.rowcount != 1:
            # Матч исчез между проверкой и записью — откатываем, чтобы не остаться
            # с событиями без результата.
            raise RuntimeError(f"Match {match_id} was not saved: UPDATE touched {cursor.rowcount} rows")
        # Кубок: результат игры двигает серию, и серия может решиться этим же
        # матчем. Скоуп тот же, что у записи счёта: подтверждённая игра с
        # нерешённой серией — это сетка, в следующий этап по которой прошёл не
        # тот клуб. Ошибка здесь откатывает подтверждение целиком.
        advance_cup_series(cursor, match_id, actor_id=reporter_id)
        try:
            settle_match_bets(match_id, p1_score, p2_score)
        except Exception as e:
            logger.warning(f"Error settling bets for match {match_id}: {e}")
        # Рейтинги и разрешение прогнозов — отдельными блоками: сбой аналитики
        # не должен отменять уже записанный результат матча.
        try:
            _apply_elo_after_match(match_id, p1_score, p2_score)
        except Exception as e:
            logger.warning(f"Error updating Elo for match {match_id}: {e}")
        try:
            resolve_ai_predictions(match_id, p1_score, p2_score)
        except Exception as e:
            logger.warning(f"Error resolving predictions for match {match_id}: {e}")
    return None
def _infer_technical_type(p1_score: int, p2_score: int) -> str:
    """Вид технического результата по счёту: ТП хозяевам, ТП гостям или ТН."""
    if p1_score > p2_score:
        return "tp_home"
    if p2_score > p1_score:
        return "tp_away"
    return "tech_draw"


def set_technical_result(
    match_id: int,
    p1_score: int,
    p2_score: int,
    technical_type: str | None = None,
) -> str | None:
    """Set a technical result (ТП / ТН) for a match.

    `technical_type` is one of 'tp_home', 'tp_away', 'tech_draw' (or inferred from score).
    The match is flagged `is_technical = 1`, its `match_events` are wiped so technical goals
    never reach the scorer tables, and every bet on the match is settled as **voided** —
    i.e. a 100% refund for singles and a 1.00 leg inside an express.
    """
    if p1_score < 0 or p2_score < 0:
        raise ValueError("Scores must be non-negative integers")
    tech_type = technical_type or _infer_technical_type(p1_score, p2_score)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE matches SET player1_score = ?, player2_score = ?, status = 'confirmed', played_at = ?, "
            "is_technical = 1, technical_type = ?, mvp_player = NULL WHERE id = ?",
            (p1_score, p2_score, now_msk_str(), tech_type, match_id)
        )
        # Тот же silent no-op, что и в confirm_and_finalize_match.
        if cursor.rowcount != 1:
            raise ValueError(f"Match {match_id} not found: technical result not saved")
        cursor.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        try:
            from services.settlement_engine import settle_match_predictions
            settle_match_predictions(match_id, p1_score, p2_score, match_status="voided")
        except Exception as e:
            logger.warning(f"Error settling bets on technical result for match {match_id}: {e}")
    return None

def save_pending_report(match_id: int, reporter_id: int, payload: dict) -> None:
    """DEPRECATED. Persist a match report payload (JSON) for later confirmation.

    Opponent confirmation was removed — a reported result is finalized on the
    spot by `confirm_and_finalize_match`, so nothing in the bot writes here any
    more. The table, the readers and this writer stay for the rows older
    databases still carry and for the purge/reset scripts that clean them.
    """
    import json
    with transaction() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO pending_reports (match_id, reporter_id, payload, created_at) VALUES (?, ?, ?, datetime('now', '+3 hours'))",
            (match_id, reporter_id, json.dumps(payload, ensure_ascii=False))
        )


def get_pending_report(match_id: int) -> dict | None:
    """DEPRECATED (see `save_pending_report`). Load a stored report payload.

    Returns a dict with reporter_id and the parsed payload fields.
    """
    import json
    with transaction() as conn:
        row = conn.execute(
            "SELECT reporter_id, payload FROM pending_reports WHERE match_id = ?", (match_id,)
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data.update(json.loads(data.pop("payload")))
        except Exception:
            return None
        return data


def delete_pending_report(match_id: int) -> None:
    """Remove a stored report payload. Still called when a result is finalized,
    to clear rows parked by builds that predate the removal of confirmation."""
    with transaction() as conn:
        conn.execute("DELETE FROM pending_reports WHERE match_id = ?", (match_id,))


def reset_match(match_id: int) -> None:
    """Reset match status to pending, clear scores/events, and update cup series if cup match."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT cup_series_id, status FROM matches WHERE id = ?", (match_id,))
        m = cursor.fetchone()
        s_id = m["cup_series_id"] if m else None

        # cup_winner_team — ответ о послематчевых для снятого результата. Оставь
        # его, и переигровку вничью молча засчитали бы прежнему победителю.
        cursor.execute(
            "UPDATE matches SET status = 'pending', player1_score = NULL, player2_score = NULL, reported_by = NULL, photo_id = NULL, dispute_photos = NULL, mvp_player = NULL, cup_winner_team = NULL WHERE id = ?",
            (match_id,)
        )
        # A fresh start also drops any freeze state from the previous result
        cursor.execute(
            "UPDATE matches SET is_extended = 0, frozen_at = NULL, frozen_seconds = 0 WHERE id = ?",
            (match_id,)
        )
        cursor.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        # Reset debt lifecycle: recorded warn milestones and pending confirmation reports
        cursor.execute("DELETE FROM debt_reminders WHERE match_id = ?", (match_id,))
        # Долг снова открыт: срок и выданная награда сохраняются (повторно её
        # не дадут), вердикт и стадии трекера — сбрасываются.
        cursor.execute(
            "UPDATE match_debts SET state = 'active', verdict_applied_at = NULL, "
            "resolved_at = NULL, resolution = NULL, resolved_by = NULL, "
            "last_reminder_at = NULL, soft_warned_at = NULL, escalated_at = NULL, last_escalation_at = NULL, escalation_count = 0, global_escalated_at = NULL "
            "WHERE match_id = ? AND state != 'cancelled'",
            (match_id,)
        )
        cursor.execute("DELETE FROM pending_reports WHERE match_id = ?", (match_id,))

        if s_id:
            # Победы в серии пересчитываются тем же кодом, что и при подтверждении
            # игры: по полю победителя, а не сравнением голов. Из сравнения голов
            # 2:2 не даёт победителя вовсе, и после сброса одной игры серия
            # потеряла бы засчитанную победу.
            cursor.execute("SELECT team1_name, team2_name FROM cup_series WHERE id = ?", (s_id,))
            if cursor.fetchone():
                try:
                    state = recompute_cup_series(cursor, s_id)
                except ValueError as e:
                    logger.warning("Cup series #%s not recalculated after reset of match #%s: %s", s_id, match_id, e)
                else:
                    # Серия, решённая и без сброшенной игры (2:1 → 2:0), остаётся
                    # решённой, и ставки на заголовок сразу пересчитываются под
                    # новый счёт. Ставшая нерешённой — открывается, а игры, снятые
                    # её досрочным концом, возвращаются в расписание; заголовок
                    # вернуть в pending нельзя, его приводит к серии следующее
                    # решение (`_sync_cup_series_header` из `advance_cup_series`).
                    cursor.execute(
                        "UPDATE cup_series SET team1_wins = ?, team2_wins = ?, winner_name = ?, "
                        "winner_source = ?, status = ? WHERE id = ?",
                        (
                            state["team1_wins"], state["team2_wins"], state["winner_name"],
                            state["winner_source"], "completed" if state["decided"] else "active", s_id,
                        )
                    )
                    if state["decided"]:
                        _sync_cup_series_header(cursor, s_id, state)
                    else:
                        _reopen_cup_series_games(cursor, s_id)

def propose_match_time(match_id: int, user_id: int, time_str: str) -> None:
    """Propose or update match time by player."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE matches SET proposed_time = ?, proposed_by = ?, time_status = 'proposed' WHERE id = ?",
            (time_str, user_id, match_id)
        )

def accept_match_time(match_id: int) -> None:
    """Accept the proposed match time."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE matches SET time_status = 'accepted' WHERE id = ?",
            (match_id,)
        )

def save_match_events(match_id: int, events: list[tuple[str, str, int]], team_name: str = None) -> None:
    """Insert match events cleanly, aggregating counts and deleting previous events for specified team or match."""
    if not events:
        return
    with transaction() as conn:
        cursor = conn.cursor()
        if team_name:
            cursor.execute("DELETE FROM match_events WHERE match_id = ? AND LOWER(team_name) = LOWER(?)", (match_id, team_name.strip()))
        else:
            t_names = set(item[0].strip() for item in events)
            for tn in t_names:
                cursor.execute("DELETE FROM match_events WHERE match_id = ? AND LOWER(team_name) = LOWER(?)", (match_id, tn.lower()))

        aggregated = {}
        for item in events:
            t_name = item[0].strip()
            p_name = item[1].strip()
            e_type = item[2]
            cnt = item[3] if len(item) > 3 else 1
            key = (t_name, p_name, e_type)
            aggregated[key] = aggregated.get(key, 0) + cnt

        for (t_name, p_name, e_type), cnt in aggregated.items():
            cursor.execute(
                "INSERT INTO match_events (match_id, team_name, player_name, event_type, count) VALUES (?, ?, ?, ?, ?)",
                (match_id, t_name, p_name, e_type, cnt)
            )

def get_active_match_by_teams(team1: str, team2: str, caption: str | None = None, division_id: int | None = None, exclude_ids: set | list | None = None) -> dict | None:
    """
    Find an active (pending/reported/disputed) match given two team names, optional caption, and optional division_id.

    exclude_ids отсеивает матчи, которые вызывающий код уже занял: скоринг
    детерминированный, поэтому без этого несколько игр одной серии сматчились бы
    на один и тот же match_id.
    """
    if not team1 or not team2:
        return None

    excluded = {int(x) for x in exclude_ids} if exclude_ids else set()

    t1_canon = resolve_team_name(team1) or team1
    t2_canon = resolve_team_name(team2) or team2
    t1_lower = t1_canon.lower().strip()
    t2_lower = t2_canon.lower().strip()
    
    caption_clean = (caption or "").lower()
    
    # Detect Cup keywords (including typos like 'кубак')
    cup_keywords = [
        "кубок", "кубак", "кубк", "кубка", "кубке", "cup",
        "1/8", "1/4", "1/2", "полуфинал", "финал", "плей-офф", "плейофф", "playoff", "1/16"
    ]
    is_cup_hint = any(w in caption_clean for w in cup_keywords)
    
    # Detect specific Round number (e.g. "16 тур", "тур 16", "25 тур")
    round_match = re.search(r'(?:тур|турн|round|r|т|раунд)\s*[:\.\-—#]?\s*(\d+)', caption_clean)
    if not round_match:
        round_match = re.search(r'(\d+)\s*[:\.\-—#]?\s*(?:тур|round|раунд)', caption_clean)
    target_round = int(round_match.group(1)) if round_match else None
    
    now = now_msk()

    active_season = get_active_season()
    active_season_id = active_season["id"] if active_season else 1

    with transaction() as conn:
        cursor = conn.cursor()
        # `rounds` уникален по (season_id, division_id, round_number): джойн только
        # по round_number подтягивал is_open/deadline произвольного дивизиона или
        # сезона, из-за чего скоринг кандидатов считал чужой дедлайн. Матчи тоже
        # ограничены активным сезоном — иначе незакрытая игра прошлого сезона
        # могла выиграть матчинг у текущей.
        cursor.execute("""
            SELECT 
                m.id, m.status, m.tournament_type, m.round_number, m.cup_stage, m.cup_series_id, m.game_num_in_series,
                m.division_id,
                m.player1_team AS direct_p1_team, m.player2_team AS direct_p2_team,
                u1.team_name AS u1_team, u2.team_name AS u2_team,
                COALESCE(r.is_open, 0) AS is_round_open,
                r.deadline AS round_deadline,
                r.id AS round_row_id,
                r.status AS round_status,
                COALESCE(s.status, '') AS series_status,
                (SELECT COUNT(*) FROM match_events me WHERE me.match_id = m.id) AS events_count
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            LEFT JOIN rounds r
                   ON m.round_number = r.round_number
                  AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
                  AND r.division_id = COALESCE(m.division_id, 1)
                  AND r.season_id = COALESCE(m.season_id, ?)
            LEFT JOIN cup_series s ON m.cup_series_id = s.id
            WHERE (m.season_id = ? OR m.season_id IS NULL)
              AND (
                    m.status IN ('pending', 'reported', 'disputed')
                    OR (m.status = 'confirmed' AND (SELECT COUNT(*) FROM match_events me WHERE me.match_id = m.id) = 0)
              )
        """, (active_season_id, active_season_id))
        rows = cursor.fetchall()
        
        candidates = []
        for row in rows:
            d = dict(row)
            if d['id'] in excluded:
                continue
            p1 = (d['direct_p1_team'] or d['u1_team'] or "").lower()
            p2 = (d['direct_p2_team'] or d['u2_team'] or "").lower()
            
            if not p1 or not p2:
                continue
            
            # Substring match for team names using normalized matching
            is_match = False
            if (teams_match(t1_lower, p1) and teams_match(t2_lower, p2)) or \
               (teams_match(t1_lower, p2) and teams_match(t2_lower, p1)):
                is_match = True
                
            if is_match:
                # If division_id is specified, enforce division boundary unless it's a cup match hint
                if division_id is not None and not is_cup_hint:
                    if d.get("division_id") != division_id:
                        continue

                score = 0
                t_type = d.get('tournament_type') or 'league'

                # Тур, который ещё не открывали, результатов не принимает. Повторный
                # скриншот уже сыгранного матча иначе уезжал в следующую игру той же
                # пары: подтверждённый матч выпадает из кандидатов, и скоринг брал
                # лучшее из оставшегося — расписание будущего тура. Закрытый тур
                # проверке не подлежит: в нём остаются долги, и их доигрывают.
                if t_type == 'league' and d.get('round_row_id') is not None:
                    round_state = {
                        "is_open": d.get('is_round_open'),
                        "status": d.get('round_status'),
                        "deadline": d.get('round_deadline'),
                    }
                    if debt_policy.round_status(round_state) == debt_policy.ROUND_SCHEDULED:
                        continue

                is_pending = d['status'] in ('pending', 'reported', 'disputed')
                is_technical = (d['status'] == 'confirmed' and d.get('events_count', 0) == 0)
                
                # Check if deadline is expired for open rounds (Case 1)
                is_deadline_expired = False
                dl_str = d.get('round_deadline')
                if dl_str:
                    dl_dt = parse_flexible_datetime(dl_str)
                    if dl_dt and dl_dt <= now:
                        is_deadline_expired = True
                
                if is_cup_hint:
                    if t_type == 'cup':
                        if is_pending:
                            score += 2500
                            if d.get('series_status') == 'active':
                                score += 500
                        elif is_technical:
                            score += 1500
                        else:
                            score += 1000
                    else:
                        score -= 1000
                else:
                    if target_round is not None:
                        if t_type == 'league' and d.get('round_number') == target_round:
                            if is_pending:
                                score += 3000
                            elif is_technical:
                                score += 2500  # Case 2: replace TP/TN in specified round
                            else:
                                score += 2000
                        elif t_type == 'league':
                            score -= 500
                    else:
                        if t_type == 'league':
                            rn = d.get('round_number', 50)
                            rn_bonus = max(0, 100 - rn)
                            
                            if is_pending:
                                if d.get('is_round_open') == 1:
                                    if is_deadline_expired:
                                        # Case 1: Open round + deadline expired (active debt!)
                                        score += 700 + rn_bonus
                                    else:
                                        # Open round + deadline not expired
                                        score += 500 + rn_bonus
                                else:
                                    # Closed round — незакрытая игра там уже долг
                                    score += 50 + rn_bonus
                            elif is_technical:
                                # Case 2: Closed/open round where admin set TP/TN
                                if d.get('is_round_open') == 0:
                                    score += 350 + rn_bonus  # Closed round with TP/TN
                                else:
                                    score += 300 + rn_bonus
                        elif t_type == 'cup' and d.get('series_status') == 'active':
                            score += 300

                candidates.append((score, d['id']))
                
        if not candidates:
            return None
            
        candidates.sort(key=lambda x: x[0], reverse=True)
        best_match_id = candidates[0][1]
        return get_match(best_match_id)

def get_match_events(match_id: int) -> list[dict]:
    """Retrieve all events (goals/assists) for a match, aggregated by team, player, and event_type."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT team_name, player_name, event_type, SUM(count) AS count FROM match_events WHERE match_id = ? GROUP BY team_name, player_name, event_type",
            (match_id,)
        )
        return [dict(row) for row in cursor.fetchall()]

def get_matches_in_rounds(round_numbers: list[int], division_id: int | None = None) -> list[dict]:
    """Retrieve all matches in a list of rounds with player details, optionally filtered by division."""
    if not round_numbers:
        return []
    placeholders = ",".join(["?"] * len(round_numbers))
    div_clause = " AND (m.division_id = ? OR m.division_id IS NULL)" if division_id is not None else ""
    params = tuple(round_numbers) + ((division_id,) if division_id is not None else ())
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT
                m.id, m.round_number, m.division_id,
                COALESCE(m.player1_id, u1_id.telegram_id, u1_team.telegram_id) AS player1_id,
                COALESCE(m.player2_id, u2_id.telegram_id, u2_team.telegram_id) AS player2_id,
                m.status,
                COALESCE(u1_id.username, u1_team.username) AS player1_username,
                COALESCE(u1_id.team_name, u1_team.team_name, m.player1_team) AS player1_team,
                COALESCE(u2_id.username, u2_team.username) AS player2_username,
                COALESCE(u2_id.team_name, u2_team.team_name, m.player2_team) AS player2_team
            FROM matches m
            LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
            LEFT JOIN users u1_team ON LOWER(m.player1_team) = LOWER(u1_team.team_name) AND (u1_team.division_id = m.division_id OR m.division_id IS NULL)
            LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
            LEFT JOIN users u2_team ON LOWER(m.player2_team) = LOWER(u2_team.team_name) AND (u2_team.division_id = m.division_id OR m.division_id IS NULL)
            WHERE m.round_number IN ({placeholders}){div_clause}
            ORDER BY m.round_number ASC, m.id ASC
        """, params)
        return [dict(row) for row in cursor.fetchall()]

def get_unplayed_matches_by_round(round_number: int, division_id: int | None = None) -> list[dict]:
    """Retrieve unplayed (pending) matches in a specific round with player details, optionally filtered by division."""
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute("""
                SELECT 
                    m.id, m.round_number, m.division_id,
                    COALESCE(m.player1_id, u1_id.telegram_id, u1_team.telegram_id) AS player1_id,
                    COALESCE(m.player2_id, u2_id.telegram_id, u2_team.telegram_id) AS player2_id,
                    m.status,
                    COALESCE(u1_id.username, u1_team.username) AS player1_username,
                    COALESCE(u1_id.team_name, u1_team.team_name, m.player1_team) AS player1_team,
                    COALESCE(u2_id.username, u2_team.username) AS player2_username,
                    COALESCE(u2_id.team_name, u2_team.team_name, m.player2_team) AS player2_team
                FROM matches m
                LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
                LEFT JOIN users u1_team ON LOWER(m.player1_team) = LOWER(u1_team.team_name) AND (u1_team.division_id = m.division_id OR m.division_id IS NULL)
                LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
                LEFT JOIN users u2_team ON LOWER(m.player2_team) = LOWER(u2_team.team_name) AND (u2_team.division_id = m.division_id OR m.division_id IS NULL)
                WHERE m.round_number = ? AND m.status = 'pending' AND (m.division_id = ? OR m.division_id IS NULL)
                ORDER BY m.id ASC
            """, (round_number, division_id))
        else:
            cursor.execute("""
                SELECT 
                    m.id, m.round_number, m.division_id,
                    COALESCE(m.player1_id, u1_id.telegram_id, u1_team.telegram_id) AS player1_id,
                    COALESCE(m.player2_id, u2_id.telegram_id, u2_team.telegram_id) AS player2_id,
                    m.status,
                    COALESCE(u1_id.username, u1_team.username) AS player1_username,
                    COALESCE(u1_id.team_name, u1_team.team_name, m.player1_team) AS player1_team,
                    COALESCE(u2_id.username, u2_team.username) AS player2_username,
                    COALESCE(u2_id.team_name, u2_team.team_name, m.player2_team) AS player2_team
                FROM matches m
                LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
                LEFT JOIN users u1_team ON LOWER(m.player1_team) = LOWER(u1_team.team_name) AND (u1_team.division_id = m.division_id OR m.division_id IS NULL)
                LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
                LEFT JOIN users u2_team ON LOWER(m.player2_team) = LOWER(u2_team.team_name) AND (u2_team.division_id = m.division_id OR m.division_id IS NULL)
                WHERE m.round_number = ? AND m.status = 'pending'
                ORDER BY m.id ASC
            """, (round_number,))
        return [dict(row) for row in cursor.fetchall()]

def get_open_rounds_with_deadlines(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Retrieve all open rounds that have a deadline set, optionally filtered by division and season."""
    with transaction() as conn:
        cursor = conn.cursor()
        query = "SELECT round_number, deadline, division_id, season_id FROM rounds WHERE is_open = 1 AND deadline IS NOT NULL AND deadline != ''"
        params = []
        if division_id is not None:
            query += " AND division_id = ?"
            params.append(division_id)
        if season_id is not None:
            query += " AND season_id = ?"
            params.append(season_id)
        else:
            act = get_active_season()
            if act:
                query += " AND (season_id = ? OR season_id IS NULL)"
                params.append(act["id"])
        cursor.execute(query, tuple(params))
        return [dict(row) for row in cursor.fetchall()]


def get_rounds_pending_preview(season_id: int | None = None) -> list[dict]:
    """Open rounds with a deadline whose АНАЛИТИКА preview has not been posted yet."""
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        cursor.execute("""
            SELECT r.division_id, r.round_number, r.season_id, r.deadline
            FROM rounds r
            LEFT JOIN round_content_posts p
                   ON p.division_id = r.division_id
                  AND p.round_number = r.round_number
                  AND p.content_type = 'preview'
            WHERE r.is_open = 1
              AND r.deadline IS NOT NULL
              AND r.deadline != ''
              AND (r.season_id = ? OR r.season_id IS NULL)
              AND p.round_number IS NULL
            ORDER BY r.division_id, r.round_number
        """, (target_season_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_rounds_pending_digest(season_id: int | None = None) -> list[dict]:
    """
    Finished rounds whose АНАЛИТИКА digest has not been posted yet.

    A round qualifies only once every match of it is confirmed (technical
    results are confirmed too); cancelled matches do not count. Closing the
    round is not enough: its unplayed matches become debts and are still to be
    played or judged, and a digest posted before that crowns a player of the
    round over half a round. `/round_digest N` remains the manual override.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        cursor.execute("""
            SELECT r.division_id,
                   r.round_number,
                   r.season_id,
                   COUNT(m.id) AS matches_total,
                   SUM(CASE WHEN m.status = 'confirmed' THEN 1 ELSE 0 END) AS matches_confirmed
            FROM rounds r
            JOIN matches m
              ON m.round_number = r.round_number
             AND m.division_id = r.division_id
             AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
             AND (m.season_id = r.season_id OR m.season_id IS NULL)
             AND m.status != 'cancelled'
            LEFT JOIN round_content_posts p
                   ON p.division_id = r.division_id
                  AND p.round_number = r.round_number
                  AND p.content_type = 'digest'
            WHERE (r.season_id = ? OR r.season_id IS NULL)
              AND p.round_number IS NULL
            GROUP BY r.division_id, r.round_number, r.season_id
            HAVING matches_confirmed > 0
               AND matches_confirmed = matches_total
            ORDER BY r.division_id, r.round_number
        """, (target_season_id,))
        return [dict(row) for row in cursor.fetchall()]


def get_teams_recent_form(limit: int = 5, division_id: int | None = None, season_id: int | None = None) -> dict[str, list[str]]:
    """
    Retrieve the last `limit` confirmed match outcomes for each team by team_name.
    Returns dict mapping lowercase team_name -> list of 'W', 'D', 'L' outcomes.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        if division_id is not None:
            cursor.execute("""
                SELECT player1_team, player2_team, player1_score, player2_score
                FROM matches
                WHERE status = 'confirmed' 
                  AND (tournament_type IS NULL OR tournament_type = 'league')
                  AND division_id = ?
                  AND (season_id = ? OR season_id IS NULL)
                ORDER BY round_number DESC, id DESC
            """, (division_id, target_season_id))
            all_matches = cursor.fetchall()

            cursor.execute("SELECT team_name FROM users WHERE division_id = ? AND team_name IS NOT NULL AND team_name != ''", (division_id,))
            team_candidates = [r["team_name"] for r in cursor.fetchall()]
        else:
            cursor.execute("""
                SELECT player1_team, player2_team, player1_score, player2_score
                FROM matches
                WHERE status = 'confirmed'
                  AND (tournament_type IS NULL OR tournament_type = 'league')
                  AND (season_id = ? OR season_id IS NULL)
                ORDER BY round_number DESC, id DESC
            """, (target_season_id,))
            all_matches = cursor.fetchall()

            cursor.execute("SELECT team_name FROM users WHERE team_name IS NOT NULL AND team_name != ''")
            team_candidates = [r["team_name"] for r in cursor.fetchall()]

        form_map = {}
        for t in team_candidates:
            canon = resolve_team_name(t) or t
            outcomes = []
            for r in all_matches:
                p1 = r["player1_team"] or ""
                p2 = r["player2_team"] or ""
                s1, s2 = r["player1_score"], r["player2_score"]
                if s1 is None or s2 is None:
                    continue
                if teams_match(p1, canon):
                    if s1 > s2: outcomes.append('W')
                    elif s1 < s2: outcomes.append('L')
                    else: outcomes.append('D')
                elif teams_match(p2, canon):
                    if s2 > s1: outcomes.append('W')
                    elif s2 < s1: outcomes.append('L')
                    else: outcomes.append('D')
                if len(outcomes) >= limit:
                    break
            
            reversed_outcomes = list(reversed(outcomes))
            form_map[canon.lower()] = reversed_outcomes
            if t.lower() != canon.lower():
                form_map[t.lower()] = reversed_outcomes
        return form_map

def has_reminder_been_sent(round_number: int, reminder_type: str, division_id: int | None = None) -> bool:
    """Check if a specific reminder type (24h, 6h, 1h) has already been sent for a round."""
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute(
                "SELECT 1 FROM round_reminders WHERE round_number = ? AND reminder_type = ? AND division_id = ?",
                (round_number, reminder_type, division_id)
            )
        else:
            cursor.execute(
                "SELECT 1 FROM round_reminders WHERE round_number = ? AND reminder_type = ?",
                (round_number, reminder_type)
            )
        return cursor.fetchone() is not None


def get_sent_reminder_tags(round_number: int, division_id: int) -> set[str]:
    """Все теги напоминаний, уже отправленных по туру дивизиона."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT reminder_type FROM round_reminders WHERE round_number = ? AND division_id = ?",
            (round_number, division_id)
        ).fetchall()
        return {r["reminder_type"] for r in rows}


def record_reminders_sent(round_number: int, tags: list[str], division_id: int) -> None:
    """Пометить несколько вех напоминаний разом (догон пропущенных)."""
    now = now_msk_str()
    with transaction() as conn:
        for tag in tags:
            conn.execute(
                "INSERT OR IGNORE INTO round_reminders (division_id, round_number, reminder_type, sent_at) "
                "VALUES (?, ?, ?, ?)",
                (division_id, round_number, tag, now)
            )


def clear_all_rounds_and_matches() -> None:
    """Completely wipe all rounds, matches, reminders, and match events from DB."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM match_events")
        cursor.execute("DELETE FROM round_reminders")
        cursor.execute("DELETE FROM match_debts")
        cursor.execute("DELETE FROM matches")
        cursor.execute("DELETE FROM rounds")

def create_round(round_number: int, deadline: str = None, division_id: int | None = None) -> None:
    """Create a new round in DB if it doesn't already exist (closed by default)."""
    div_id = division_id if division_id is not None else 1
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR IGNORE INTO rounds (division_id, round_number, is_open, deadline) VALUES (?, ?, 0, ?)",
            (div_id, round_number, deadline)
        )

def _team_names_by_telegram_id(cursor, player_ids: "set[int] | list[int]") -> dict[int, str]:
    """Клубы участников по их telegram_id; коучи без клуба в словарь не попадают.

    Матч связан с users через имя клуба, поэтому при создании матча его нужно
    зафиксировать в самой строке — см. комментарий в `batch_insert_matches`.
    """
    ids = [int(p) for p in player_ids if p]
    if not ids:
        return {}
    placeholders = ",".join("?" * len(ids))
    cursor.execute(
        f"SELECT telegram_id, team_name FROM users WHERE telegram_id IN ({placeholders})",
        ids
    )
    return {r["telegram_id"]: r["team_name"] for r in cursor.fetchall() if r["team_name"]}


def create_match(round_number: int, player1_id: int, player2_id: int, division_id: int | None = None) -> int:
    """Create a new pending match between two players in a round."""
    if player1_id == player2_id:
        raise ValueError("Игрок не может играть сам с собой (player1_id == player2_id).")

    with transaction() as conn:
        cursor = conn.cursor()
        team_by_id = _team_names_by_telegram_id(cursor, (player1_id, player2_id))
        teams = list(team_by_id.values())
        if len(teams) == 2 and teams[0].lower() == teams[1].lower():
            raise ValueError(f"Команды участников совпадают: {teams[0]}")

        cursor.execute(
            "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (round_number, player1_id, player2_id,
             team_by_id.get(player1_id), team_by_id.get(player2_id), division_id)
        )
        return cursor.lastrowid

def record_reminder_sent(round_number: int, reminder_type: str, division_id: int | None = None) -> None:
    """Mark a specific reminder type as sent for a round."""
    with transaction() as conn:
        cursor = conn.cursor()
        div_id = division_id if division_id is not None else 1
        cursor.execute(
            "INSERT OR REPLACE INTO round_reminders (division_id, round_number, reminder_type, sent_at) VALUES (?, ?, ?, datetime('now', '+3 hours'))",
            (div_id, round_number, reminder_type)
        )


def has_round_content_post(division_id: int, round_number: int, content_type: str) -> bool:
    """Whether the preview/digest for this round has already been published."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM round_content_posts WHERE division_id = ? AND round_number = ? AND content_type = ?",
            (division_id, round_number, content_type)
        )
        return cursor.fetchone() is not None


def record_round_content_post(division_id: int, round_number: int, content_type: str, message_id: int | None = None) -> None:
    """Mark the preview/digest for this round as published."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO round_content_posts (division_id, round_number, content_type, message_id, posted_at) VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))",
            (division_id, round_number, content_type, message_id)
        )


def clear_round_content_post(division_id: int, round_number: int, content_type: str) -> None:
    """Drop the published marker so the content can be regenerated (manual admin re-run)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM round_content_posts WHERE division_id = ? AND round_number = ? AND content_type = ?",
            (division_id, round_number, content_type)
        )


def admin_set_match_score(match_id: int, player1_score: int, player2_score: int, admin_id: int | None = None) -> None:
    """Manually set match score and confirm it by admin, clearing any previous match events."""
    if player1_score < 0 or player2_score < 0:
        raise ValueError("Scores must be non-negative integers")
    closed = cup_results_closed_reason(match_id)
    if closed:
        raise ValueError(closed)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT player1_score, player2_score, status, division_id, season_id, COALESCE(is_technical, 0) AS is_technical FROM matches WHERE id = ?", (match_id,))
        old_m = cursor.fetchone()

        is_correction = bool(old_m and old_m["status"] == "confirmed" and old_m["player1_score"] is not None and old_m["player2_score"] is not None)
        old_score_str = f"{old_m['player1_score']}:{old_m['player2_score']}" if is_correction else None

        cursor.execute("DELETE FROM match_events WHERE match_id = ?", (match_id,))
        # Корона пришла с того же скриншота, что и снесённые события: после
        # ручной правки счёта она осталась бы наградой за отменённый разбор.
        cursor.execute(
            "UPDATE matches SET player1_score = ?, player2_score = ?, status = 'confirmed', "
            "played_at = ?, mvp_player = NULL WHERE id = ?",
            (player1_score, player2_score, now_msk_str(), match_id)
        )

        if is_correction and admin_id:
            div_id = old_m["division_id"] if old_m else None
            s_id = old_m["season_id"] if old_m else None
            try:
                log_admin_action(
                    admin_id=admin_id,
                    action="correct_match_score",
                    target_type="match",
                    target_id=match_id,
                    old_value=old_score_str,
                    new_value=f"{player1_score}:{player2_score}",
                    reason="Admin corrected confirmed match score",
                    division_id=div_id,
                    season_id=s_id
                )
            except Exception as e:
                logger.warning(f"Could not log admin score correction for match {match_id}: {e}")

        score_changed = is_correction and (old_m["player1_score"], old_m["player2_score"]) != (player1_score, player2_score)
        match_status = "voided" if old_m and old_m["is_technical"] else "finished"
        try:
            if score_changed:
                # Ставки по матчу уже рассчитаны по старому счёту: settle_match_bets
                # трогает только pending и оставил бы старые выигрыши. Пересчёт
                # отзывает прежние выплаты и начисляет по исправленному счёту.
                from services.settlement_engine import resettle_match_predictions
                resettle_match_predictions(match_id, player1_score, player2_score, match_status=match_status)
            else:
                settle_match_bets(match_id, player1_score, player2_score, match_status=match_status)
        except Exception as e:
            logger.warning(f"Error settling bets in admin_set_match_score for match {match_id}: {e}")

        # Тот же счёт — та же аналитика. _apply_elo_after_match сам откатит свою
        # прошлую дельту, поэтому повторный вызов не сдвигает рейтинг дважды.
        try:
            _apply_elo_after_match(match_id, player1_score, player2_score)
        except Exception as e:
            logger.warning(f"Error updating Elo in admin_set_match_score for match {match_id}: {e}")
        try:
            if score_changed:
                correct_ai_predictions(match_id, player1_score, player2_score)
            else:
                resolve_ai_predictions(match_id, player1_score, player2_score)
        except Exception as e:
            logger.warning(f"Error resolving predictions in admin_set_match_score for match {match_id}: {e}")

def get_config(key: str) -> str | None:
    """Retrieve a configuration value by key."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM system_config WHERE key = ?", (key,))
        row = cursor.fetchone()
        return row[0] if row else None

def set_config(key: str, value: str) -> None:
    """Insert or update a configuration key-value pair."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "REPLACE INTO system_config (key, value) VALUES (?, ?)",
            (key, value)
        )

AI_CHAT_ENABLED_KEY = "ai_chat_enabled"


def is_ai_chat_enabled() -> bool:
    """
    Мастер-выключатель генеративных ответов ИИ «Темшик».
    Отсутствие записи трактуется как «включено» — выключение хранится явным "0".
    """
    return get_config(AI_CHAT_ENABLED_KEY) != "0"


def set_ai_chat_enabled(enabled: bool) -> None:
    """Сохранить состояние мастер-выключателя ИИ «Темшик»."""
    set_config(AI_CHAT_ENABLED_KEY, "1" if enabled else "0")


def get_group_id() -> int | None:
    """Retrieve the automatically tracked Telegram Group ID."""
    val = get_config("group_id")
    try:
        return int(val) if val else None
    except ValueError:
        return None


def get_chat_history(user_id: int, limit: int = 10) -> list[dict]:
    """Retrieve recent AI chat history for a user (oldest first)."""
    with transaction() as conn:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute(
            "SELECT role, text FROM ("
            "  SELECT id, role, text FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (user_id, limit)
        )
        rows = cursor.fetchall()
        return [{"role": r["role"], "text": r["text"]} for r in rows]


def append_chat_history(user_id: int, role: str, text: str) -> None:
    """Append one message to the AI chat history."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO chat_history (user_id, role, text, created_at) VALUES (?, ?, ?, datetime('now', '+3 hours'))",
            (user_id, role, text)
        )


def trim_chat_history(user_id: int, keep: int = 10) -> None:
    """Delete oldest AI chat history rows beyond the newest `keep` for a user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM chat_history WHERE user_id = ? AND id NOT IN ("
            "  SELECT id FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT ?"
            ")",
            (user_id, user_id, keep)
        )


def append_style_sample(text: str) -> None:
    """Append one real message of the persona source user (e.g. @t3miy)."""
    text_clean = text.strip()
    if not text_clean:
        return
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO style_samples (text, created_at) VALUES (?, datetime('now', '+3 hours'))",
            (text_clean,)
        )


def get_style_samples(limit: int = 20) -> list[str]:
    """Return the newest style samples (oldest first)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT text FROM ("
            "  SELECT id, text FROM style_samples ORDER BY id DESC LIMIT ?"
            ") ORDER BY id ASC",
            (limit,)
        )
        return [r["text"] for r in cursor.fetchall()]


def trim_style_samples(keep: int = 100) -> None:
    """Keep only the newest `keep` style samples."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM style_samples WHERE id NOT IN ("
            "  SELECT id FROM style_samples ORDER BY id DESC LIMIT ?"
            ")",
            (keep,)
        )


def _count_round_matches(cursor, round_number: int, division_id: int | None, season_id: int) -> int:
    """Сколько матчей стоит в расписании тура внутри (season, division).

    Единственная точка, где считается «есть ли у тура расписание»: на неё
    опираются и гейт открытия тура, и выставление линии. У легаси-строк матча
    `division_id` может быть NULL — по принятому в проекте соглашению это
    дивизион 1; NULL в `season_id` относится к запрошенному сезону, ровно как
    в `get_matches_by_round`, чтобы гейт не запрещал открыть тур, матчи
    которого админ видит в карточке тура.
    """
    if division_id is not None:
        cursor.execute(
            "SELECT COUNT(*) AS c FROM matches WHERE round_number = ? "
            "AND COALESCE(division_id, 1) = ? AND (season_id = ? OR season_id IS NULL)",
            (round_number, division_id, season_id)
        )
    else:
        cursor.execute(
            "SELECT COUNT(*) AS c FROM matches WHERE round_number = ? "
            "AND (season_id = ? OR season_id IS NULL)",
            (round_number, season_id)
        )
    return cursor.fetchone()["c"] or 0


def count_round_matches(round_number: int, division_id: int | None = None, season_id: int | None = None) -> int:
    """Публичная обёртка над `_count_round_matches` для хендлеров."""
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    with transaction() as conn:
        return _count_round_matches(conn.cursor(), round_number, division_id, s_id)


def _resolve_season_id(season_id: int | None) -> int:
    """Явный сезон или активный; 1 — когда активного сезона в БД ещё нет."""
    if season_id is not None:
        return season_id
    act = get_active_season()
    return act["id"] if act else 1


def _active_open_rounds(cursor, division_id: int, season_id: int) -> list[dict]:
    """Туры дивизиона, которые прямо сейчас занимают слот открытия.

    Единственная точка, где решается «тур ещё активен или уже нет»: слот держит
    `is_open = 1` вместе с дедлайном строго в будущем. Тур с истёкшим дедлайном
    остаётся в БД открытым, но слот освобождает — ручное закрытие админом не
    требуется. Тур без разбираемого дедлайна (NULL или мусор) слот не занимает:
    иначе один такой тур блокировал бы дивизион навсегда.
    """
    cursor.execute(
        "SELECT round_number, is_open, deadline, division_id, season_id FROM rounds "
        "WHERE is_open = 1 AND division_id = ? AND (season_id = ? OR season_id IS NULL) "
        "ORDER BY round_number",
        (division_id, season_id)
    )
    now = now_msk()
    active: list[dict] = []
    for row in cursor.fetchall():
        dt = parse_flexible_datetime(row["deadline"])
        if dt and dt > now:
            active.append(dict(row))
    return active


def get_active_open_rounds(division_id: int, season_id: int | None = None) -> list[dict]:
    """Открытые туры дивизиона с неистекшим дедлайном, по возрастанию номера."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        return _active_open_rounds(conn.cursor(), division_id, s_id)


def _assert_rounds_within_limit(cursor, round_numbers: list[int], division_id: int, season_id: int) -> None:
    """Гейт лимита: проверяет, влезут ли `round_numbers` в свободные слоты.

    Туры, которые уже среди активных, слот не занимают повторно — продление или
    смена дедлайна уже открытого тура проходит свободно. Проверка идёт внутри
    транзакции вызывающего и ДО первой записи, чтобы отказ не оставлял следов.
    """
    active = _active_open_rounds(cursor, division_id, season_id)
    active_numbers = [r["round_number"] for r in active]
    incoming = [r for r in round_numbers if r not in set(active_numbers)]
    if not incoming:
        return
    if len(active_numbers) + len(incoming) > MAX_OPEN_ROUNDS_PER_DIVISION:
        raise MaxActiveRoundsExceededError(division_id, active_numbers, MAX_OPEN_ROUNDS_PER_DIVISION)


def get_next_rounds_to_open(
    division_id: int,
    season_id: int | None = None,
    count: int = MAX_OPEN_ROUNDS_PER_DIVISION,
) -> list[int]:
    """Следующие `count` туров дивизиона, готовых к открытию.

    Отсчёт идёт от максимального когда-либо открытого тура — открытого сейчас
    или уже закрытого (истёкший дедлайн значения не имеет), поэтому после туров 1–2 сразу
    предлагаются 3–4. Если не открывался ни один — `[1, 2]`. Туры без
    сгенерированного расписания отбрасываются: открыть их всё равно нельзя.
    """
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT MAX(round_number) AS max_r FROM rounds "
            "WHERE (is_open = 1 OR status = 'closed') AND division_id = ? AND (season_id = ? OR season_id IS NULL)",
            (division_id, s_id)
        )
        row = cursor.fetchone()
        max_r = (row["max_r"] if row else None) or 0

        return [
            r_num
            for r_num in range(max_r + 1, max_r + 1 + count)
            if _count_round_matches(cursor, r_num, division_id, s_id) > 0
        ]


def open_rounds_batch(
    start_round: int,
    end_round: int,
    deadline: str,
    division_id: int | None = None,
    season_id: int | None = None,
) -> dict:
    """Open multiple rounds and set a shared deadline, scoped by division and season.

    Туры без расписания пропускаются: открыть тур, для которого не сгенерированы
    матчи, нельзя (см. `RoundScheduleMissingError`). Возвращает отчёт
    `{"opened": [...], "skipped": [...]}` — вызывающий обязан показать админу
    пропущенные туры. Если расписания нет ни у одного тура диапазона, не
    изменяется ничего.
    """
    div_id = division_id if division_id is not None else 1
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    act = get_season(s_id)
    if act and act.get("status") not in ("active", None):
        raise ValueError(f"Cannot open rounds in season #{s_id} with status '{act.get('status')}'")

    # Сериализуется с приёмом ставок — см. update_round_status.
    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()

        # Сначала — раскладка диапазона на «есть расписание / нет расписания».
        # Проверка идёт до первой записи, поэтому пустой тур не получает ни
        # строки в rounds, ни is_open = 1.
        opened: list[int] = []
        skipped: list[int] = []
        for r_num in range(start_round, end_round + 1):
            if _count_round_matches(cursor, r_num, div_id if division_id is not None else None, s_id) > 0:
                opened.append(r_num)
            else:
                skipped.append(r_num)

        # Лимит считается по турам, которые реально откроются: пропущенные из-за
        # отсутствия расписания слотов не занимают. Проверка — до первой записи,
        # поэтому превышение не открывает даже часть диапазона.
        if opened:
            if division_id is not None:
                _assert_rounds_within_limit(cursor, opened, div_id, s_id)
            else:
                for scope_div_id in sorted({
                    d
                    for r_num in opened
                    for d in _round_scope_divisions(cursor, r_num, s_id)
                }):
                    _assert_rounds_within_limit(cursor, opened, scope_div_id, s_id)

        for r_num in opened:
            cursor.execute(
                "INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 0, NULL)",
                (s_id, div_id, r_num)
            )
            if division_id is not None:
                cursor.execute(
                    "UPDATE rounds SET is_open = 1, deadline = ? WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                    (deadline, s_id, division_id, r_num)
                )
                cursor.execute(
                    "UPDATE rounds SET bets_open = 0, bets_opened_at = NULL WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                    (s_id, division_id, r_num)
                )
            else:
                cursor.execute(
                    "UPDATE rounds SET is_open = 1, deadline = ? WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                    (deadline, s_id, r_num)
                )
                cursor.execute(
                    "UPDATE rounds SET bets_open = 0, bets_opened_at = NULL WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                    (s_id, r_num)
                )

        # 🎰 Тур, открытый для игры, ставки не принимает: линия закрывается
        # в обеих схемах, а не генерируется. То же правило, что в update_round_status.
        # Каждое закрытие идёт в пределах конкретного (season, division, round):
        # открытие туров Дивизиона 1 не гасит линию Дивизиона 2 и других сезонов.
        for r_num in opened:
            if division_id is not None:
                close_round_betting_line(cursor, r_num, division_id=division_id, season_id=s_id)
            else:
                for scope_div_id in _round_scope_divisions(cursor, r_num, s_id):
                    close_round_betting_line(cursor, r_num, division_id=scope_div_id, season_id=s_id)

        now = now_msk()
        for r_num in opened:
            scope = [division_id] if division_id is not None else _round_scope_divisions(cursor, r_num, s_id)
            for scope_div_id in scope:
                cursor.execute(
                    "DELETE FROM round_reminders WHERE round_number = ? AND division_id = ?",
                    (r_num, scope_div_id)
                )
                _after_round_opened(cursor, r_num, scope_div_id, s_id, deadline, now)

    if skipped:
        logger.warning(
            f"open_rounds_batch: rounds {skipped} skipped (no schedule) "
            f"for division={division_id}, season={s_id}"
        )

    # 🎰 Парный цикл «два через два»: открытые для игры туры ушли из линии —
    # автоматически выставляем её на два следующих тура.
    advanced = []
    if opened:
        advanced = advance_betting_line_pair(division_id=division_id, season_id=s_id)

    return {"opened": opened, "skipped": skipped, "advanced": advanced}

def get_open_pending_matches() -> list[dict]:
    """Get all pending matches where the round is open and not extended, scoped by division and season."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT 
                m.id, m.round_number, m.division_id, m.season_id, u1.telegram_id AS player1_id, u2.telegram_id AS player2_id, m.status, m.is_extended,
                u1.username AS player1_nickname, u1.team_name AS player1_team,
                u2.username AS player2_nickname, u2.team_name AS player2_team,
                r.deadline
            FROM matches m
            JOIN rounds r ON m.round_number = r.round_number
                         AND (m.division_id = r.division_id OR (m.division_id IS NULL AND r.division_id = 1))
                         AND (m.season_id = r.season_id OR (m.season_id IS NULL AND (r.season_id = 1 OR r.season_id IS NULL)))
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.status = 'pending' 
              AND m.is_extended = 0
              AND r.is_open = 1
            ORDER BY m.round_number ASC, m.id ASC
        """)
        return [dict(row) for row in cursor.fetchall()]

def extend_match_deadline(match_id: int) -> int:
    """Toggle is_extended between 0 and 1 for an overdue match. Returns new value.
    Freeze time is accumulated so auto-warn schedules shift, not skip."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_extended FROM matches WHERE id = ?", (match_id,))
        row = cursor.fetchone()
        cur = row["is_extended"] if row and row["is_extended"] else 0
        new_val = 0 if cur == 1 else 1
        _apply_freeze_state(cursor, match_id, new_val)
        return new_val


def set_match_extended(match_id: int, value: int) -> None:
    """Explicitly set is_extended (1 = freeze auto-warns, 0 = resume). No toggle.
    Freeze intervals are recorded so that hours_overdue excludes frozen time."""
    with transaction() as conn:
        cursor = conn.cursor()
        _apply_freeze_state(cursor, match_id, 1 if value else 0)


def _apply_freeze_state(cursor: sqlite3.Cursor, match_id: int, new_val: int) -> None:
    """Shared freeze bookkeeping: stamp frozen_at on freeze, accumulate elapsed
    seconds into frozen_seconds on unfreeze."""
    now_str = now_msk_str()
    if new_val == 1:
        # Start freezing: remember when (idempotent if already frozen)
        cursor.execute(
            "UPDATE matches SET is_extended = 1, "
            "frozen_at = COALESCE(frozen_at, ?) WHERE id = ?",
            (now_str, match_id)
        )
    else:
        # Resume: bank the elapsed frozen interval, then clear the marker
        cursor.execute("SELECT frozen_at, COALESCE(frozen_seconds, 0) AS fs FROM matches WHERE id = ?", (match_id,))
        row = cursor.fetchone()
        extra = 0
        if row and row["frozen_at"]:
            f_at = parse_flexible_datetime(row["frozen_at"])
            if f_at:
                extra = max(0, int((now_msk() - f_at).total_seconds()))
        cursor.execute(
            "UPDATE matches SET is_extended = 0, frozen_at = NULL, "
            "frozen_seconds = COALESCE(frozen_seconds, 0) + ? WHERE id = ?",
            (extra, match_id)
        )


def extend_match_deadline_by_hours(match_id: int, hours: int) -> str | None:
    """Grant a debt match a fixed extension of `hours` (24 or 48).

    Freezes the debt clock (`is_extended = 1`) and stamps `extended_until` with
    the moment it resumes. The *round* deadline is deliberately left alone: it
    is shared by every match of the round, so shifting it would silently extend
    matches the admin never touched. The per-match freeze already shifts this
    match's overdue timestamps by exactly the extension length.

    The 48h admin escalation stage is cleared so the admin is asked again once
    the extension runs out. Returns the new expiry as a string.
    """
    if hours <= 0:
        raise ValueError("Extension length must be positive")
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM matches WHERE id = ?", (match_id,))
        if not cursor.fetchone():
            return None

        _apply_freeze_state(cursor, match_id, 1)
        until = now_msk() + datetime.timedelta(hours=hours)
        until_str = until.strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("UPDATE matches SET extended_until = ? WHERE id = ?", (until_str, match_id))
        cursor.execute(
            "UPDATE match_debts SET escalated_at = NULL, last_escalation_at = NULL, "
            "global_escalated_at = NULL, "
            "state = CASE WHEN state = 'escalated' THEN 'active' ELSE state END "
            "WHERE match_id = ?",
            (match_id,)
        )
        return until_str


def get_match_extension_expiry(match_id: int) -> datetime.datetime | None:
    """When the current admin extension of a debt match runs out (None if none)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT extended_until FROM matches WHERE id = ?", (match_id,))
        row = cursor.fetchone()
        if not row or not row["extended_until"]:
            return None
        return parse_flexible_datetime(row["extended_until"])


def expire_match_extension(match_id: int) -> None:
    """Resume the debt clock after an extension ran out.

    Banks the frozen interval into `frozen_seconds` (so the overdue clock is
    shifted, not skipped) and clears `extended_until`.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        _apply_freeze_state(cursor, match_id, 0)
        cursor.execute("UPDATE matches SET extended_until = NULL WHERE id = ?", (match_id,))


def get_match_frozen_seconds(match_id: int) -> float:
    """Total seconds the match has spent frozen, INCLUDING the current ongoing
    freeze interval (if is_extended=1 right now)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT is_extended, frozen_at, COALESCE(frozen_seconds, 0) AS fs FROM matches WHERE id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
        if not row:
            return 0.0
        total = float(row["fs"] or 0)
        if row["is_extended"] and row["frozen_at"]:
            f_at = parse_flexible_datetime(row["frozen_at"])
            if f_at:
                total += max(0.0, (now_msk() - f_at).total_seconds())
        return total

def get_matches_by_round(round_number: int, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Retrieve all matches for a specific round with player details, optionally filtered by division.

    `season_id` включён в выборку намеренно: вызывающие (в частности
    `services.betting_engine.generate_round_markets`) досеивают матчи по сезону
    в Python, и без этой колонки их фильтр молча пропускал матчи чужих сезонов.

    Тур сам по себе не уникален: номер повторяется в каждом сезоне, поэтому
    выборка по дивизиону тоже ограничена сезоном (по умолчанию — активным).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            target_season_id = season_id
            if target_season_id is None:
                act = get_active_season()
                target_season_id = act["id"] if act else 1
            cursor.execute("""
                SELECT
                    m.id, m.round_number, m.division_id, m.season_id, u1.telegram_id AS player1_id, u2.telegram_id AS player2_id,
                    m.player1_score, m.player2_score, m.status, m.mvp_player,
                    COALESCE(u1.username, u1_id.username) AS player1_nickname,
                    COALESCE(u1.username, u1_id.username) AS player1_username,
                    COALESCE(m.player1_team, u1.team_name, u1_id.team_name, 'Команда 1') AS player1_team,
                    COALESCE(u2.username, u2_id.username) AS player2_nickname,
                    COALESCE(u2.username, u2_id.username) AS player2_username,
                    COALESCE(m.player2_team, u2.team_name, u2_id.team_name, 'Команда 2') AS player2_team
                FROM matches m
                LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
                LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
                LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
                LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
                WHERE m.round_number = ? AND COALESCE(m.division_id, 1) = ?
                  AND (m.season_id = ? OR m.season_id IS NULL)
                ORDER BY m.id ASC
            """, (round_number, division_id, target_season_id))
        else:
            act = get_active_season()
            s_id = act["id"] if act else 1
            cursor.execute("""
                SELECT
                    m.id, m.round_number, m.division_id, m.season_id, u1.telegram_id AS player1_id, u2.telegram_id AS player2_id,
                    m.player1_score, m.player2_score, m.status, m.mvp_player,
                    COALESCE(u1.username, u1_id.username) AS player1_nickname,
                    COALESCE(u1.username, u1_id.username) AS player1_username,
                    COALESCE(m.player1_team, u1.team_name, u1_id.team_name, 'Команда 1') AS player1_team,
                    COALESCE(u2.username, u2_id.username) AS player2_nickname,
                    COALESCE(u2.username, u2_id.username) AS player2_username,
                    COALESCE(m.player2_team, u2.team_name, u2_id.team_name, 'Команда 2') AS player2_team
                FROM matches m
                LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
                LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
                LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
                LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
                WHERE m.round_number = ?
                  AND (m.season_id = ? OR m.season_id IS NULL)
                  AND (m.division_id = 1 OR m.division_id IS NULL)
                ORDER BY m.id ASC
            """, (round_number, s_id))
        rows = [dict(row) for row in cursor.fetchall()]
        for d in rows:
            if not d.get("player1_username") and d.get("player1_team"):
                u = find_user_by_team(d["player1_team"])
                if u:
                    d["player1_username"] = u.get("username")
                    d["player1_nickname"] = u.get("username")
            if not d.get("player2_username") and d.get("player2_team"):
                u = find_user_by_team(d["player2_team"])
                if u:
                    d["player2_username"] = u.get("username")
                    d["player2_nickname"] = u.get("username")
        return rows

def get_admins() -> list[dict]:
    """Retrieve all users with admin role."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, username FROM users WHERE role = 'admin'")
        return [dict(row) for row in cursor.fetchall()]

def pre_register_player(username: str, team_name: str) -> int:
    """Pre-register a player with a temporary negative ID."""
    username_clean = username.strip().lstrip("@")
    team_name_clean = team_name.strip()
    with transaction() as conn:
        cursor = conn.cursor()
        
        # Previous owners are read first: the UPDATE clears team_name, after which
        # a subquery keyed on it matches nobody and their warns would outlive the
        # warn_count reset. Same ordering rule as in `set_player_club`.
        cursor.execute(
            "SELECT telegram_id FROM users WHERE LOWER(team_name) = LOWER(?) AND LOWER(username) != LOWER(?)",
            (team_name_clean, username_clean)
        )
        previous_owner_ids = [r["telegram_id"] for r in cursor.fetchall()]
        cursor.execute(
            "UPDATE users SET team_name = NULL, warn_count = 0 WHERE LOWER(team_name) = LOWER(?) AND LOWER(username) != LOWER(?)",
            (team_name_clean, username_clean)
        )
        for owner_id in previous_owner_ids:
            cursor.execute("DELETE FROM user_warns WHERE user_id = ?", (owner_id,))

        # Check if username already exists in users table
        cursor.execute("SELECT telegram_id FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
        row = cursor.fetchone()
        if row:
            # Update club name, reset warns, set player role
            cursor.execute(
                "UPDATE users SET team_name = ?, role = 'player', warn_count = 0 WHERE LOWER(username) = LOWER(?)",
                (team_name_clean, username_clean)
            )
            cursor.execute("DELETE FROM user_warns WHERE user_id = ?", (row[0],))
            return row[0]
        
        # Generate a new unique negative ID for pre-registration
        cursor.execute("SELECT MIN(telegram_id) FROM users")
        min_row = cursor.fetchone()
        min_id = min_row[0] if min_row and min_row[0] else 0
        temp_id = min(min_id - 1, -1)
        
        cursor.execute(
            "INSERT INTO users (telegram_id, username, team_name, league_name, role, warn_count, registered_at) VALUES (?, ?, ?, ?, ?, 0, datetime('now', '+3 hours'))",
            (temp_id, username_clean, team_name_clean, "Основная", "player")
        )
        return temp_id

def pre_register_player_to_division(username: str, division_id: int) -> int:
    """Pre-register or update a player by username into a division without a club assigned."""
    username_clean = username.strip().lstrip("@")
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
        row = cursor.fetchone()
        if row:
            tg_id = row[0]
            cursor.execute(
                "UPDATE users SET division_id = ?, role = CASE WHEN role = 'admin' THEN 'admin' ELSE 'player' END WHERE telegram_id = ?",
                (division_id, tg_id)
            )
            return tg_id
        
        cursor.execute("SELECT MIN(telegram_id) FROM users")
        min_row = cursor.fetchone()
        min_id = min_row[0] if min_row and min_row[0] else 0
        temp_id = min(min_id - 1, -1)
        
        cursor.execute(
            "INSERT INTO users (telegram_id, username, team_name, league_name, role, division_id, warn_count, registered_at) "
            "VALUES (?, ?, NULL, 'Основная', 'player', ?, 0, datetime('now', '+3 hours'))",
            (temp_id, username_clean, division_id)
        )
        return temp_id

def _repoint_user_owned_rows(cursor, old_id: int, new_id: int) -> None:
    """
    Move the Logovo.bet rows owned by a pre-registration placeholder onto the real id.

    `matches`, `user_warns` and `pending_reports` are re-pointed by the caller. This
    covers the economy side, where `user_wallets` and `user_progression` keep
    `user_id` as their PRIMARY KEY: when the real user already owns a row, the two
    have to be merged rather than moved, or the UPDATE hits a constraint and the
    placeholder's coins and XP are lost with its `users` row.

    `squad_players` deliberately has no user column — it is keyed by `team_name`,
    which travels with the merged user record on its own, so nothing to do there.
    """
    # ── Wallet ───────────────────────────────────────────────────────────────
    cursor.execute("SELECT * FROM user_wallets WHERE user_id = ?", (old_id,))
    old_wallet = cursor.fetchone()
    if old_wallet:
        cursor.execute("SELECT 1 FROM user_wallets WHERE user_id = ?", (new_id,))
        if cursor.fetchone():
            # Both sides hold a wallet, so the welcome bonus was granted twice.
            # Carry over only what the placeholder earned on top of it and drop the
            # duplicate grant: balance must stay equal to the sum of the ledger.
            # Стартовый баланс настраивается в панели, поэтому вычитаем ровно
            # ту сумму, что была начислена, а не нынешнюю настройку.
            cursor.execute(
                "SELECT id, amount FROM coin_transactions"
                " WHERE user_id = ? AND transaction_type = 'welcome_bonus'"
                " ORDER BY id LIMIT 1",
                (old_id,)
            )
            welcome = cursor.fetchone()
            duplicate_bonus = 0
            if welcome:
                cursor.execute("DELETE FROM coin_transactions WHERE id = ?", (welcome["id"],))
                duplicate_bonus = welcome["amount"]
            cursor.execute(
                """
                UPDATE user_wallets SET
                    balance = balance + ?,
                    total_wagered = total_wagered + ?,
                    total_won = total_won + ?,
                    bets_count = bets_count + ?,
                    bets_won = bets_won + ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE user_id = ?
                """,
                (
                    old_wallet["balance"] - duplicate_bonus,
                    old_wallet["total_wagered"],
                    old_wallet["total_won"],
                    old_wallet["bets_count"],
                    old_wallet["bets_won"],
                    new_id,
                )
            )
            cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (old_id,))
        else:
            cursor.execute("UPDATE user_wallets SET user_id = ? WHERE user_id = ?", (new_id, old_id))

    # ── Progression ──────────────────────────────────────────────────────────
    cursor.execute("SELECT * FROM user_progression WHERE user_id = ?", (old_id,))
    old_prog = cursor.fetchone()
    if old_prog:
        cursor.execute("SELECT total_xp_earned FROM user_progression WHERE user_id = ?", (new_id,))
        new_prog = cursor.fetchone()
        if new_prog:
            # Keep whichever profile actually earned more: an untouched placeholder
            # is a fresh level-1 row and must never overwrite real progress.
            if old_prog["total_xp_earned"] > new_prog["total_xp_earned"]:
                cursor.execute(
                    """
                    UPDATE user_progression SET
                        level = ?, current_xp = ?, total_xp_earned = ?,
                        current_streak = ?, best_streak = ?, last_active_date = ?,
                        streak_shields = ?, equipped_frame = ?, equipped_title = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE user_id = ?
                    """,
                    (
                        old_prog["level"], old_prog["current_xp"], old_prog["total_xp_earned"],
                        old_prog["current_streak"], old_prog["best_streak"], old_prog["last_active_date"],
                        old_prog["streak_shields"], old_prog["equipped_frame"], old_prog["equipped_title"],
                        new_id,
                    )
                )
            cursor.execute("DELETE FROM user_progression WHERE user_id = ?", (old_id,))
        else:
            cursor.execute("UPDATE user_progression SET user_id = ? WHERE user_id = ?", (new_id, old_id))

    # ── Bets ─────────────────────────────────────────────────────────────────
    # user_bets carries UNIQUE(user_id, idempotency_key) for non-NULL keys: release
    # any key the real user already holds so the placeholder's bet survives the move.
    cursor.execute(
        """
        UPDATE user_bets SET idempotency_key = NULL
        WHERE user_id = ? AND idempotency_key IS NOT NULL
          AND idempotency_key IN (SELECT idempotency_key FROM user_bets WHERE user_id = ?)
        """,
        (old_id, new_id)
    )
    cursor.execute("UPDATE user_bets SET user_id = ? WHERE user_id = ?", (new_id, old_id))

    # ── Coin ledger ──────────────────────────────────────────────────────────
    # Plain column, no uniqueness — a straight move is always safe. Runs last so the
    # duplicate welcome bonus above is removed while it still belongs to old_id.
    cursor.execute("UPDATE coin_transactions SET user_id = ? WHERE user_id = ?", (new_id, old_id))


def handle_user_startup(telegram_id: int, username: str | None, default_role: str = 'user') -> None:
    """
    Handle a user starting the bot.
    If the user has a pre-registered entry (matched by username), update their telegram_id
    and matches referencing their temporary ID. Otherwise, perform a standard upsert.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        
        # Check if there is a pre-registered user with this username (negative id)
        pre_reg = None
        if username:
            cursor.execute(
                "SELECT * FROM users WHERE LOWER(username) = LOWER(?) AND telegram_id < 0",
                (username.strip(),)
            )
            pre_reg = cursor.fetchone()

        # 1. Check if the exact telegram_id already exists
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
        exists = cursor.fetchone()
        
        if exists:
            if pre_reg:
                old_id = pre_reg['telegram_id']
                new_team = exists['team_name'] or pre_reg['team_name']
                new_league = exists['league_name'] or pre_reg['league_name']
                new_role = pre_reg['role'] if exists['role'] == 'user' else exists['role']
                new_notif = 1 if (pre_reg['team_name'] and not exists['team_name']) else exists['pending_notification']
                new_division = exists['division_id'] if ('division_id' in exists.keys() and exists['division_id'] is not None) else (pre_reg['division_id'] if 'division_id' in pre_reg.keys() else None)
                
                # Free team_name on old record to prevent UNIQUE constraint conflict
                cursor.execute("UPDATE users SET team_name = NULL WHERE telegram_id = ?", (old_id,))
                
                # Re-point references to real telegram_id (foreign keys valid since exists is already in users)
                cursor.execute("UPDATE matches SET player1_id = ? WHERE player1_id = ?", (telegram_id, old_id))
                cursor.execute("UPDATE matches SET player2_id = ? WHERE player2_id = ?", (telegram_id, old_id))
                cursor.execute("UPDATE matches SET reported_by = ? WHERE reported_by = ?", (telegram_id, old_id))
                cursor.execute("UPDATE matches SET proposed_by = ? WHERE proposed_by = ?", (telegram_id, old_id))
                cursor.execute("UPDATE user_warns SET user_id = ? WHERE user_id = ?", (telegram_id, old_id))
                cursor.execute("UPDATE pending_reports SET reporter_id = ? WHERE reporter_id = ?", (telegram_id, old_id))
                _repoint_user_owned_rows(cursor, old_id, telegram_id)

                # Delete old temporary record
                cursor.execute("DELETE FROM users WHERE telegram_id = ?", (old_id,))
                
                cursor.execute(
                    "UPDATE users SET username = ?, team_name = ?, league_name = ?, role = ?, pending_notification = ?, division_id = ? WHERE telegram_id = ?",
                    (username, new_team, new_league, new_role, new_notif, new_division, telegram_id)
                )
                logger.info(f"Merged pre-registered user @{username} (old_id: {old_id}) into existing user {telegram_id}")
            else:
                cursor.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id)
                )
            return

        if pre_reg:
            old_id = pre_reg['telegram_id']
            old_team = pre_reg['team_name']
            
            # 1. Clear team_name on old record so inserting new record doesn't violate unique constraint
            cursor.execute("UPDATE users SET team_name = NULL WHERE telegram_id = ?", (old_id,))
            
            # 2. Insert new user record first so foreign keys (matches, user_warns) can reference real telegram_id
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, league_name, role, registered_at, pending_notification, warn_count, squad_photo_id, division_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    telegram_id, username, old_team, pre_reg['league_name'], 
                    pre_reg['role'], pre_reg['registered_at'], 1 if old_team else 0,
                    pre_reg['warn_count'] if 'warn_count' in pre_reg.keys() else 0,
                    pre_reg['squad_photo_id'] if 'squad_photo_id' in pre_reg.keys() else None,
                    pre_reg['division_id'] if 'division_id' in pre_reg.keys() else None
                )
            )
            
            # 3. Re-point references to real telegram_id
            cursor.execute("UPDATE matches SET player1_id = ? WHERE player1_id = ?", (telegram_id, old_id))
            cursor.execute("UPDATE matches SET player2_id = ? WHERE player2_id = ?", (telegram_id, old_id))
            cursor.execute("UPDATE matches SET reported_by = ? WHERE reported_by = ?", (telegram_id, old_id))
            cursor.execute("UPDATE matches SET proposed_by = ? WHERE proposed_by = ?", (telegram_id, old_id))
            cursor.execute("UPDATE user_warns SET user_id = ? WHERE user_id = ?", (telegram_id, old_id))
            cursor.execute("UPDATE pending_reports SET reporter_id = ? WHERE reporter_id = ?", (telegram_id, old_id))
            _repoint_user_owned_rows(cursor, old_id, telegram_id)

            # 4. Delete old temporary record
            cursor.execute("DELETE FROM users WHERE telegram_id = ?", (old_id,))
            
            logger.info(f"Matched pre-registered user @{username} (old_id: {old_id}) to real id: {telegram_id}")
            return

        # 3. Fallback: regular insert
        cursor.execute(
            "INSERT INTO users (telegram_id, username, role, registered_at) VALUES (?, ?, ?, datetime('now', '+3 hours'))",
            (telegram_id, username, default_role)
        )

def remove_player(player_ref: str) -> tuple[bool, str]:
    """
    Remove player from users table after cleaning up their matches/events to prevent FK constraint failures.
    """
    player_ref_clean = player_ref.strip().lstrip("@")
    with transaction() as conn:
        cursor = conn.cursor()
        
        # Find user first
        cursor.execute("SELECT telegram_id, team_name, username FROM users WHERE telegram_id = ? OR LOWER(username) = LOWER(?)", (player_ref_clean, player_ref_clean))
        row = cursor.fetchone()
        if not row:
            return False, "Игрок не найден."
            
        p_id, team, uname = row[0], row[1], row[2]
        
        # Unlink player from matches instead of deleting them to preserve league history
        cursor.execute("UPDATE matches SET player1_id = NULL WHERE player1_id = ?", (p_id,))
        cursor.execute("UPDATE matches SET player2_id = NULL WHERE player2_id = ?", (p_id,))
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (p_id,))
        
        display_name = f"@{uname}" if uname else f"ID {p_id}"
        return True, f"Игрок {display_name} ({team or 'без названия'}) успешно удален из лиги."

def set_player_club(player_ref: str, new_club: str) -> tuple[bool, str]:
    """Bind a coach to a club, taking it away from its previous owner if it has one.

    `player_ref` is a @username or a telegram_id. Returns `(ok, message)`; the
    message is **plain text** — it ends up in a Telegram alert or in an HTML
    message, and neither renders Markdown.
    """
    player_ref_clean = player_ref.strip().lstrip("@")
    new_club_clean = new_club.strip()
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, username FROM users WHERE telegram_id = ? OR LOWER(username) = LOWER(?)",
            (player_ref_clean, player_ref_clean)
        )
        row = cursor.fetchone()
        if not row:
            return False, "Игрок не найден."
        p_id = row["telegram_id"]
        p_label = f"@{row['username']}" if row["username"] else f"ID {p_id}"

        cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (p_id,))
        old_club_row = cursor.fetchone()
        old_club = (old_club_row[0] or "").strip() if old_club_row else ""

        # Previous owners are read before the UPDATE below clears their team_name:
        # afterwards a `WHERE LOWER(team_name) = …` subquery matches nobody, so their
        # warn history would outlive the warn_count that was just zeroed. Their names
        # also go into the reply — the admin should see whom the club was taken from.
        cursor.execute(
            "SELECT telegram_id, username FROM users WHERE LOWER(team_name) = LOWER(?) AND telegram_id != ?",
            (new_club_clean, p_id)
        )
        previous_owners = cursor.fetchall()

        cursor.execute(
            "UPDATE users SET team_name = NULL, warn_count = 0 WHERE LOWER(team_name) = LOWER(?) AND telegram_id != ?",
            (new_club_clean, p_id)
        )
        for owner in previous_owners:
            cursor.execute("DELETE FROM user_warns WHERE user_id = ?", (owner["telegram_id"],))

        cursor.execute("UPDATE users SET team_name = ?, role = 'player', warn_count = 0 WHERE telegram_id = ?", (new_club_clean, p_id))
        cursor.execute("DELETE FROM user_warns WHERE user_id = ?", (p_id,))

        # Keep pending fixtures and active cup series pointing at the club the
        # player now owns, so they don't become orphaned debts of the old club.
        if old_club and old_club.lower() != new_club.strip().lower():
            cursor.execute(
                "UPDATE matches SET player1_team = ? WHERE status = 'pending' AND LOWER(player1_team) = LOWER(?)",
                (new_club.strip(), old_club)
            )
            cursor.execute(
                "UPDATE matches SET player2_team = ? WHERE status = 'pending' AND LOWER(player2_team) = LOWER(?)",
                (new_club.strip(), old_club)
            )
            cursor.execute(
                "UPDATE cup_series SET team1_name = ? WHERE status = 'active' AND LOWER(team1_name) = LOWER(?)",
                (new_club.strip(), old_club)
            )
            cursor.execute(
                "UPDATE cup_series SET team2_name = ? WHERE status = 'active' AND LOWER(team2_name) = LOWER(?)",
                (new_club.strip(), old_club)
            )
            # Fresh debt lifecycle for the transferred fixtures
            cursor.execute(
                "DELETE FROM debt_reminders WHERE match_id IN "
                "(SELECT id FROM matches WHERE status = 'pending' AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?)))",
                (new_club.strip(), new_club.strip())
            )
            cursor.execute(
                "UPDATE match_debts SET state = 'active', last_reminder_at = NULL, soft_warned_at = NULL, escalated_at = NULL, last_escalation_at = NULL, escalation_count = 0, global_escalated_at = NULL "
                "WHERE state IN ('active', 'escalated') AND match_id IN "
                "(SELECT id FROM matches WHERE status = 'pending' AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?)))",
                (new_club.strip(), new_club.strip())
            )

        message = f"Клуб игрока {p_label} изменён на «{new_club_clean}»."
        if previous_owners:
            taken_from = ", ".join(
                f"@{o['username']}" if o["username"] else f"ID {o['telegram_id']}"
                for o in previous_owners
            )
            message += f" Клуб отобран у {taken_from} — варны сброшены."
        return True, message


def clear_player_club(telegram_id: int) -> tuple[bool, str]:
    """Release a coach's club without removing them from the league.

    Warns go with the club: they are accrued for unplayed matches of *that* club,
    so the counter and the history are wiped together — exactly as `set_player_club`
    does when it takes a club from its previous owner. Clearing one without the
    other leaves a phantom history behind a zeroed counter.

    Pending fixtures and cup series keep the club name: they are keyed by club, and
    the next owner inherits them through `set_player_club`. Returns `(ok, message)`;
    the message is **plain text** — it ends up in a Telegram alert.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username, team_name FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        if not row:
            return False, "Игрок не найден."

        label = f"@{row['username']}" if row["username"] else f"ID {telegram_id}"
        old_club = (row["team_name"] or "").strip()
        if not old_club:
            return False, f"У игрока {label} и так нет клуба."

        cursor.execute(
            "UPDATE users SET team_name = NULL, warn_count = 0 WHERE telegram_id = ?",
            (telegram_id,)
        )
        cursor.execute("DELETE FROM user_warns WHERE user_id = ?", (telegram_id,))

        return True, f"Клуб «{old_club}» освобождён — {label} остался в лиге без клуба."


def update_player_username(telegram_id: int, username: str) -> tuple[bool, str]:
    """Update player's Telegram username."""
    username_clean = username.strip().lstrip("@")
    if not username_clean:
        return False, "Юзернейм не может быть пустым."
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM users WHERE LOWER(username) = LOWER(?) AND telegram_id != ?", (username_clean, telegram_id))
        existing = cursor.fetchone()
        if existing:
            return False, f"Юзернейм @{username_clean} уже используется другим игроком."
        cursor.execute("UPDATE users SET username = ? WHERE telegram_id = ?", (username_clean, telegram_id))
        return True, f"Telegram-юзернейм успешно изменен на @{username_clean}."

def update_player_role(telegram_id: int, role: str) -> tuple[bool, str]:
    """Update user's system role (player or admin)."""
    if role not in ("player", "admin"):
        return False, "Неверная роль."
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET role = ? WHERE telegram_id = ?", (role, telegram_id))
        return True, f"Роль успешно изменена на {role}."

def delete_player_completely(telegram_id: int) -> tuple[bool, str]:
    """Delete player completely and remove all matches involving them."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT username FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        if not row:
            return False, "Игрок не найден."
        nickname = row[0] or str(telegram_id)
        cursor.execute("""
            DELETE FROM match_events WHERE match_id IN (
                SELECT id FROM matches WHERE player1_id = ? OR player2_id = ?
            )
        """, (telegram_id, telegram_id))
        cursor.execute("DELETE FROM matches WHERE player1_id = ? OR player2_id = ?", (telegram_id, telegram_id))
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
        return True, f"Игрок @{nickname} и все матчи с его участием полностью стерты из базы данных."

def clear_entire_league() -> None:
    """Clear all matches and users (retaining users with admin role)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM match_events")
        cursor.execute("DELETE FROM round_reminders")
        cursor.execute("DELETE FROM match_debts")
        cursor.execute("DELETE FROM matches")
        cursor.execute("DELETE FROM rounds")
        cursor.execute("DELETE FROM users WHERE role != 'admin'")
        logger.info("Entire league matches and players cleared (admins kept).")


def assign_player_to_club(username: str, club: str, division_id: int = 1) -> tuple[int, str | None]:
    """
    Assign a player by username/tag to a club within a specific division.
    If the club was previously assigned to someone else in the same division, unlink (or delete) the old player.
    Returns (telegram_id, old_player_username).
    """
    username_clean = username.strip().lstrip("@")
    club_clean = club.strip()
    
    with transaction() as conn:
        cursor = conn.cursor()
        
        # 1. Find who is currently assigned to this club in this division
        cursor.execute(
            "SELECT telegram_id, username FROM users WHERE LOWER(team_name) = LOWER(?) AND division_id = ?",
            (club_clean, division_id)
        )
        old_player = cursor.fetchone()
        old_username = None
        if old_player:
            old_id, old_username = old_player[0], old_player[1]
            # Detach played matches from the old owner (preserve league history),
            # then remove their unplayed fixtures BEFORE deleting the user row
            # (FK constraint requires children gone first).
            cursor.execute(
                "UPDATE matches SET player1_id = NULL WHERE player1_id = ? AND status != 'pending'",
                (old_id,)
            )
            cursor.execute(
                "UPDATE matches SET player2_id = NULL WHERE player2_id = ? AND status != 'pending'",
                (old_id,)
            )
            cursor.execute("DELETE FROM matches WHERE player1_id = ? OR player2_id = ?", (old_id, old_id))
            cursor.execute("DELETE FROM users WHERE telegram_id = ?", (old_id,))

            # Fresh debt lifecycle for the club's remaining pending fixtures:
            # the new owner must not inherit recorded auto-warn milestones.
            cursor.execute("""
                DELETE FROM debt_reminders WHERE match_id IN (
                    SELECT id FROM matches
                    WHERE status = 'pending'
                      AND (division_id = ? OR division_id IS NULL)
                      AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?))
                )
            """, (division_id, club_clean, club_clean))
            cursor.execute("""
                UPDATE match_debts SET state = 'active', last_reminder_at = NULL, soft_warned_at = NULL, escalated_at = NULL, last_escalation_at = NULL, escalation_count = 0, global_escalated_at = NULL
                WHERE state IN ('active', 'escalated') AND match_id IN (
                    SELECT id FROM matches
                    WHERE status = 'pending'
                      AND (division_id = ? OR division_id IS NULL)
                      AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?))
                )
            """, (division_id, club_clean, club_clean))

        # 2. Check if the new player already exists in the system
        cursor.execute("SELECT telegram_id FROM users WHERE LOWER(username) = LOWER(?)", (username_clean,))
        exists = cursor.fetchone()
        if exists:
            new_id = exists[0]
            # Reset warns so a previously excluded player does not get instantly re-kicked
            cursor.execute(
                "UPDATE users SET team_name = ?, division_id = ?, warn_count = 0 WHERE telegram_id = ?",
                (club_clean, division_id, new_id)
            )
        else:
            # Generate negative temp ID
            cursor.execute("SELECT MIN(telegram_id) FROM users")
            min_row = cursor.fetchone()
            min_id = min_row[0] if min_row and min_row[0] else 0
            new_id = min(min_id - 1, -1)
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, league_name, role, division_id, registered_at) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))",
                (new_id, username_clean, club_clean, "Основная", "player", division_id)
            )
            
        return new_id, old_username



def set_pending_notification(telegram_id: int, value: int = 1) -> None:
    """Set or clear the pending notification flag for a user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET pending_notification = ? WHERE telegram_id = ?", (value, telegram_id))


def get_pending_notification(telegram_id: int) -> bool:
    """Check if a user has a pending notification."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT pending_notification FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        return bool(row and row[0])


def get_user_team(telegram_id: int) -> str | None:
    """Get the team_name assigned to a user, or None if not assigned."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (telegram_id,))
        row = cursor.fetchone()
        return row["team_name"] if row else None


def get_user_warn_count(user_id: int) -> int:
    """Get current warn count for user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT warn_count FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["warn_count"] if row and row["warn_count"] is not None else 0



def add_warn(user_id: int, admin_id: int | None, reason: str) -> tuple[int, bool]:
    from config import MAX_WARNS_LIMIT
    with transaction() as conn:
        cursor = conn.cursor()
        now_str = now_msk_str()

        # Atomic increment inside the write transaction: two concurrent callers
        # can no longer read the same warn_count and lose an increment.
        cursor.execute("UPDATE users SET warn_count = warn_count + 1 WHERE telegram_id = ?", (user_id,))
        if cursor.rowcount == 0:
            return 0, False
        cursor.execute("SELECT warn_count FROM users WHERE telegram_id = ?", (user_id,))
        new_count = cursor.fetchone()["warn_count"] or 0

        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, ?, ?, 'WARN_ADD', ?)",
            (user_id, admin_id, reason, now_str)
        )
        is_exceeded = new_count >= MAX_WARNS_LIMIT
        return new_count, is_exceeded


def remove_warn(user_id: int, admin_id: int | None, reason: str) -> tuple[int, bool]:
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT warn_count FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        current_count = row["warn_count"] if row and row["warn_count"] is not None else 0
        
        if current_count <= 0:
            return 0, False
            
        new_count = max(0, current_count - 1)
        now_str = now_msk_str()
        cursor.execute("UPDATE users SET warn_count = ? WHERE telegram_id = ?", (new_count, user_id))
        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, ?, ?, 'WARN_REMOVE', ?)",
            (user_id, admin_id, reason, now_str)
        )
        return new_count, True


def get_user_warns(user_id: int) -> list[dict]:
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, user_id, admin_id, reason, type, created_at
            FROM user_warns
            WHERE user_id = ?
            ORDER BY created_at DESC
        """, (user_id,))
        return [dict(row) for row in cursor.fetchall()]


def ban_and_remove_from_league(user_id: int) -> str | None:
    from config import MAX_WARNS_LIMIT
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        team_name = row["team_name"] if row else None

        cursor.execute("UPDATE users SET team_name = NULL, warn_count = 0 WHERE telegram_id = ?", (user_id,))
        now_str = now_msk_str()
        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, NULL, ?, 'AUTO_KICK', ?)",
            (user_id, f"Превышен лимит варнов ({MAX_WARNS_LIMIT}/{MAX_WARNS_LIMIT}). Авто-удаление из клуба.", now_str)
        )
        return team_name


def reset_season_warns() -> None:
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET warn_count = 0")
        cursor.execute("DELETE FROM user_warns")


def amnesty_player(user_id: int, admin_id: int | None = None) -> None:
    with transaction() as conn:
        cursor = conn.cursor()
        now_str = now_msk_str()
        cursor.execute("UPDATE users SET warn_count = 0 WHERE telegram_id = ?", (user_id,))
        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, ?, 'Амнистия (сброс варнов)', 'WARN_REMOVE', ?)",
            (user_id, admin_id, now_str)
        )


def reset_user_warns(user_id: int, admin_id: int | None = None) -> None:
    """Reset all warns for a specific user to 0."""
    amnesty_player(user_id, admin_id)


def find_user_by_ref(ref: str) -> dict | None:
    """Find user by telegram_id, @username, or team_name."""
    ref_clean = ref.strip().lstrip("@")
    with transaction() as conn:
        cursor = conn.cursor()
        if ref_clean.isdigit():
            cursor.execute("SELECT telegram_id, username, team_name, role, warn_count, division_id FROM users WHERE telegram_id = ?", (int(ref_clean),))
            r = cursor.fetchone()
            if r:
                return dict(r)

        cursor.execute("SELECT telegram_id, username, team_name, role, warn_count, division_id FROM users WHERE LOWER(username) = LOWER(?)", (ref_clean,))
        r = cursor.fetchone()
        if r:
            return dict(r)

        cursor.execute("SELECT telegram_id, username, team_name, role, warn_count, division_id FROM users WHERE LOWER(team_name) = LOWER(?)", (ref_clean,))
        r = cursor.fetchone()
        if r:
            return dict(r)
        return None


def get_all_active_warns() -> list[dict]:
    """Retrieve all active league users (who currently own a club) with warn_count > 0."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_id, username, team_name, warn_count 
            FROM users 
            WHERE warn_count > 0 AND team_name IS NOT NULL AND TRIM(team_name) != ''
            ORDER BY warn_count DESC, username ASC
        """)
        return [dict(row) for row in cursor.fetchall()]


# --- Squad management ---

def get_all_teams() -> list[str]:
    """Retrieve all unique team names from users table."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT team_name FROM users WHERE team_name IS NOT NULL AND team_name != ''")
        return [r[0] for r in cursor.fetchall()]

def get_team_squad_photo(team_name: str) -> str | None:
    """Retrieve squad_photo_id for a team name."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT squad_photo_id FROM users WHERE LOWER(team_name) = LOWER(?) AND squad_photo_id IS NOT NULL AND squad_photo_id != ''",
            (team_name.strip(),)
        )
        row = cursor.fetchone()
        return row[0] if row and row[0] else None

def save_squad_players(team_name: str, player_names: list) -> int:
    """Save or add players to a club squad."""
    return add_squad(team_name, player_names)

def find_player_in_squad(
    player_name: str,
    team_name: str,
    conn: sqlite3.Connection | None = None
) -> dict | None:
    """
    Find existing player in a club's squad using normalized matching and aliases.
    Strictly isolated to team_name (never matches players from other clubs).
    Returns dict: {'id': ..., 'team_name': ..., 'player_name': ..., 'position': ..., 'norm_name': ..., 'norm_team_name': ...} or None.
    """
    if not player_name or not team_name:
        return None
    p_norm = normalize_player_name_key(player_name)
    if not p_norm:
        return None
    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)

    def _search(c):
        # 1. Exact match on (norm_team_name, norm_name) (fast unique index lookup)
        c.execute("""
            SELECT id, team_name, player_name, position, norm_name, norm_team_name
            FROM squad_players
            WHERE (norm_team_name = ? OR LOWER(team_name) = LOWER(?)) AND norm_name = ?
            LIMIT 1
        """, (t_norm, t_clean, p_norm))
        row = c.fetchone()
        if row:
            return dict(row)

        # 2. Fuzzy / alias match against all players of this club
        c.execute("""
            SELECT id, team_name, player_name, position, norm_name, norm_team_name
            FROM squad_players
            WHERE norm_team_name = ? OR LOWER(team_name) = LOWER(?)
            ORDER BY id ASC
        """, (t_norm, t_clean))
        all_club_players = c.fetchall()
        for r in all_club_players:
            if is_same_footballer(player_name, r["player_name"]):
                return dict(r)
        return None

    if conn is not None:
        cursor = conn.cursor()
        return _search(cursor)
    else:
        with transaction() as local_conn:
            cursor = local_conn.cursor()
            return _search(cursor)


def _load_club_roster(cursor: sqlite3.Cursor, team_name: str | None) -> list[str]:
    """Squad player names of one club, oldest first."""
    if not team_name or not team_name.strip():
        return []
    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)
    cursor.execute(
        "SELECT player_name FROM squad_players "
        "WHERE norm_team_name = ? OR LOWER(team_name) = LOWER(?) ORDER BY id ASC",
        (t_norm, t_clean)
    )
    return [r["player_name"] for r in cursor.fetchall()]


def _squad_candidates(player_name: str, roster: list[str]) -> set[str]:
    """Squad names `player_name` may stand for: every exact-key hit, else every alias/surname hit."""
    key = normalize_player_name_key(player_name)
    if not key:
        return set()
    hits = {n for n in roster if normalize_player_name_key(n) == key}
    if not hits:
        hits = {n for n in roster if is_same_footballer(player_name, n)}
    return hits


def _canonicalize_club_player_stats(cursor: sqlite3.Cursor, team_name: str) -> int:
    """Re-point a club's match_events and matches.mvp_player spellings to its squad names.

    confirm_and_finalize_match canonicalizes names only against the squad as it is
    at confirmation time, so goals recorded before the squad was registered keep the
    OCR spelling ('Yıldız' beside the squad's 'YILDIZ') and the player is counted
    twice. Run whenever players join a squad. A name is re-pointed only when it
    stands for exactly one squad player; an ambiguous one is left as it is.
    Returns the number of match_events rows and matches renamed.
    """
    roster = _load_club_roster(cursor, team_name)
    if not roster:
        return 0
    squad_names = set(roster)
    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)

    # Events and matches keep the club under the spelling of the match row, which
    # need not be the squad's. Exact spellings are collected in Python because
    # SQLite's LOWER() folds ASCII only and would miss Cyrillic club names.
    cursor.execute("""
        SELECT team_name AS t FROM match_events
        UNION SELECT player1_team FROM matches
        UNION SELECT player2_team FROM matches
    """)
    spellings = [
        r["t"] for r in cursor.fetchall()
        if r["t"] and (
            r["t"].strip().lower() == t_clean.lower()
            or normalize_team_name(resolve_team_name(r["t"].strip()) or r["t"].strip()) == t_norm
        )
    ]

    renamed = 0
    for spelling in spellings:
        cursor.execute(
            "SELECT DISTINCT player_name FROM match_events WHERE team_name = ?", (spelling,)
        )
        for old in [r["player_name"] for r in cursor.fetchall()]:
            if not old or old in squad_names:
                continue
            hits = _squad_candidates(old, roster)
            if len(hits) != 1:
                continue
            cursor.execute(
                "UPDATE match_events SET player_name = ? WHERE team_name = ? AND player_name = ?",
                (hits.pop(), spelling, old)
            )
            renamed += cursor.rowcount

        # Two spellings of one player in one match are now two rows of one name.
        cursor.execute("""
            SELECT match_id, player_name, event_type, MIN(id) AS keep_id, SUM(count) AS total
            FROM match_events
            WHERE team_name = ?
            GROUP BY match_id, player_name, event_type
            HAVING COUNT(*) > 1
        """, (spelling,))
        for g in cursor.fetchall():
            cursor.execute("UPDATE match_events SET count = ? WHERE id = ?", (g["total"], g["keep_id"]))
            cursor.execute(
                "DELETE FROM match_events WHERE team_name = ? AND match_id = ? "
                "AND player_name = ? AND event_type = ? AND id != ?",
                (spelling, g["match_id"], g["player_name"], g["event_type"], g["keep_id"])
            )

    # matches.mvp_player carries no club, so it is renamed only when exactly one
    # player of either side answers to it.
    rosters: dict[str, list[str]] = {t_clean: roster}
    for spelling in spellings:
        cursor.execute("""
            SELECT id, mvp_player, player1_team, player2_team FROM matches
            WHERE mvp_player IS NOT NULL AND mvp_player != ''
              AND (player1_team = ? OR player2_team = ?)
        """, (spelling, spelling))
        for m in cursor.fetchall():
            old = m["mvp_player"]
            opponent = m["player2_team"] if m["player1_team"] == spelling else m["player1_team"]
            opp_key = (opponent or "").strip()
            if opp_key not in rosters:
                rosters[opp_key] = _load_club_roster(cursor, opp_key)
            opp_roster = rosters[opp_key]
            if old in squad_names or old in opp_roster:
                continue
            hits = {("own", n) for n in _squad_candidates(old, roster)}
            hits |= {("opp", n) for n in _squad_candidates(old, opp_roster)}
            if len(hits) != 1:
                continue
            cursor.execute("UPDATE matches SET mvp_player = ? WHERE id = ?", (hits.pop()[1], m["id"]))
            renamed += cursor.rowcount

    if renamed:
        logger.info("Re-pointed %d match stat rows of '%s' to squad names", renamed, t_clean)
    return renamed


def add_squad(team_name: str, player_names: list) -> int:
    """
    Add players to a club's squad with authentic positions.
    Accepts list of names: ["Vinicius Jr", ...] or tuples: [("Vinicius Jr", "LW"), ...] or dicts.
    Auto-detects authentic real-world position if not specified.
    Uses normalized upsert: does NOT create a new squad_players.id if player already exists in the club.
    """
    from services.player_positions import detect_player_position, normalize_position

    if not team_name or not player_names:
        return 0

    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)
    added = 0
    with transaction() as conn:
        cursor = conn.cursor()
        for item in player_names:
            pos = None
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                name, pos = item[0], item[1]
            elif isinstance(item, dict):
                name = item.get("player_name") or item.get("name")
                pos = item.get("position") or item.get("pos")
            else:
                name = str(item)

            clean = name.strip() if name else ""
            if not clean or len(clean) > 50:
                continue

            pos_clean = normalize_position(pos) if pos else detect_player_position(clean, t_clean)
            norm_key = normalize_player_name_key(clean)
            if not norm_key:
                continue

            existing = find_player_in_squad(clean, t_clean, conn=conn)
            if existing:
                # Player already exists in club: do NOT create new ID!
                # Enrich position if existing row has none and new pos is detected
                if not existing.get("position") and pos_clean:
                    cursor.execute(
                        "UPDATE squad_players SET position = ? WHERE id = ?",
                        (pos_clean, existing["id"])
                    )
                if not existing.get("norm_name") or not existing.get("norm_team_name"):
                    cursor.execute(
                        "UPDATE squad_players SET norm_name = ?, norm_team_name = ? WHERE id = ?",
                        (norm_key, t_norm, existing["id"])
                    )
                continue

            try:
                cursor.execute(
                    """
                    INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) 
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(team_name, player_name) DO UPDATE SET
                        position = COALESCE(excluded.position, squad_players.position),
                        norm_name = COALESCE(excluded.norm_name, squad_players.norm_name),
                        norm_team_name = COALESCE(excluded.norm_team_name, squad_players.norm_team_name)
                    """,
                    (t_clean, clean, pos_clean, norm_key, t_norm)
                )
                if cursor.rowcount > 0:
                    added += 1
            except sqlite3.IntegrityError:
                logger.info("Player '%s' already exists in club '%s' (unique index)", clean, t_clean)
            except sqlite3.Error as e:
                logger.warning(f"Failed to add player '{clean}' to {t_clean}: {e}")
        _canonicalize_club_player_stats(cursor, t_clean)
    return added


def get_squad(team_name: str) -> list[str]:
    """Get list of player names in a club's squad."""
    if not team_name:
        return []
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT player_name FROM squad_players WHERE LOWER(team_name) = LOWER(?) ORDER BY id ASC",
            (team_name.strip(),)
        )
        res = [row["player_name"] for row in cursor.fetchall()]
        if res:
            return res
        
        # Fallback with fuzzy matching
        cursor.execute("SELECT DISTINCT team_name FROM squad_players")
        all_t = [r["team_name"] for r in cursor.fetchall()]
        for t in all_t:
            if teams_match(t, team_name):
                cursor.execute("SELECT player_name FROM squad_players WHERE team_name = ? ORDER BY id ASC", (t,))
                return [row["player_name"] for row in cursor.fetchall()]
        return []


def get_squad_with_positions(team_name: str) -> list[dict]:
    """Get list of players with their positions: [{'player_name': ..., 'position': ...}]."""
    from services.player_positions import detect_player_position

    if not team_name:
        return []
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT player_name, position FROM squad_players WHERE LOWER(team_name) = LOWER(?) ORDER BY id ASC",
            (team_name.strip(),)
        )
        rows = cursor.fetchall()
        if not rows:
            cursor.execute("SELECT DISTINCT team_name FROM squad_players")
            all_t = [r["team_name"] for r in cursor.fetchall()]
            for t in all_t:
                if teams_match(t, team_name):
                    cursor.execute("SELECT player_name, position FROM squad_players WHERE team_name = ? ORDER BY id ASC", (t,))
                    rows = cursor.fetchall()
                    break

        result = []
        for r in rows:
            p_name = r["player_name"]
            pos = r["position"]
            if not pos:
                pos = detect_player_position(p_name, team_name)
                # Auto-heal position in background DB
                cursor.execute(
                    "UPDATE squad_players SET position = ? WHERE LOWER(team_name) = LOWER(?) AND LOWER(player_name) = LOWER(?)",
                    (pos, team_name.strip(), p_name.strip())
                )
            result.append({"player_name": p_name, "position": pos})
        return result


def get_player_position(player_name: str, team_name: str | None = None) -> str:
    """
    Get the authentic position of a player (e.g. ST, LW, RW, CAM, CB, GK).
    Checks DB squad_players, resolves canonical/online position, and caches in DB.
    """
    from services.player_positions import detect_player_position, normalize_position

    if not player_name:
        return "ST"

    p_clean = player_name.strip()
    with transaction() as conn:
        cursor = conn.cursor()
        if team_name:
            cursor.execute(
                "SELECT position FROM squad_players WHERE LOWER(player_name) = LOWER(?) AND LOWER(team_name) = LOWER(?)",
                (p_clean, team_name.strip())
            )
            row = cursor.fetchone()
            if row and row["position"]:
                return row["position"]

        cursor.execute(
            "SELECT position FROM squad_players WHERE LOWER(player_name) = LOWER(?) AND position IS NOT NULL LIMIT 1",
            (p_clean,)
        )
        row = cursor.fetchone()
        if row and row["position"]:
            return row["position"]

        # Resolve position dynamically
        pos = detect_player_position(p_clean, team_name)
        if team_name and pos:
            cursor.execute(
                "UPDATE squad_players SET position = ? WHERE LOWER(player_name) = LOWER(?) AND LOWER(team_name) = LOWER(?)",
                (pos, p_clean, team_name.strip())
            )
        return pos


def set_player_position(player_name: str, team_name: str, position: str) -> bool:
    """Set or update the authentic position for a player in a squad."""
    from services.player_positions import normalize_position

    norm_pos = normalize_position(position)
    norm_key = normalize_player_name_key(player_name)
    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)
    with transaction() as conn:
        cursor = conn.cursor()
        existing = find_player_in_squad(player_name, team_name, conn=conn)
        if existing:
            cursor.execute(
                "UPDATE squad_players SET position = ?, norm_name = ?, norm_team_name = ? WHERE id = ?",
                (norm_pos, norm_key, t_norm, existing["id"])
            )
            return True
        else:
            cursor.execute(
                """
                INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(team_name, player_name) DO UPDATE SET 
                    position = excluded.position,
                    norm_name = excluded.norm_name,
                    norm_team_name = excluded.norm_team_name
                """,
                (t_clean, player_name.strip(), norm_pos, norm_key, t_norm)
            )
            inserted = cursor.rowcount > 0
            _canonicalize_club_player_stats(cursor, t_clean)
            return inserted


def clear_squad(team_name: str) -> int:
    """Remove all players from a club's squad. Returns count of deleted players."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM squad_players WHERE LOWER(team_name) = LOWER(?)",
            (team_name.strip(),)
        )
        return cursor.rowcount


def replace_squad(team_name: str, player_names: list) -> tuple[int, int]:
    """
    Synchronize/replace a club's squad with `player_names` via upsert.
    Preserves canonical squad_players.id for existing players instead of deleting and recreating them.
    Returns (deleted, added).
    """
    from services.player_positions import detect_player_position, normalize_position

    if not team_name:
        return 0, 0

    t_clean = team_name.strip()
    t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)
    added = 0
    deleted = 0

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, player_name, position, norm_name, norm_team_name FROM squad_players WHERE norm_team_name = ? OR LOWER(team_name) = LOWER(?)",
            (t_norm, t_clean)
        )
        current_roster = [dict(r) for r in cursor.fetchall()]
        retained_ids = set()

        for item in player_names:
            pos = None
            if isinstance(item, (tuple, list)) and len(item) >= 2:
                name, pos = item[0], item[1]
            elif isinstance(item, dict):
                name = item.get("player_name") or item.get("name")
                pos = item.get("position") or item.get("pos")
            else:
                name = str(item)

            clean = name.strip() if name else ""
            if not clean or len(clean) > 50:
                continue

            pos_clean = normalize_position(pos) if pos else detect_player_position(clean, t_clean)
            norm_key = normalize_player_name_key(clean)
            if not norm_key:
                continue

            matched_player = None
            for p in current_roster:
                if p["id"] in retained_ids:
                    continue
                if p.get("norm_name") == norm_key or is_same_footballer(clean, p["player_name"]):
                    matched_player = p
                    break

            if matched_player:
                p_id = matched_player["id"]
                retained_ids.add(p_id)
                if pos_clean and pos_clean != matched_player.get("position"):
                    cursor.execute("UPDATE squad_players SET position = ? WHERE id = ?", (pos_clean, p_id))
                if not matched_player.get("norm_name") or not matched_player.get("norm_team_name"):
                    cursor.execute("UPDATE squad_players SET norm_name = ?, norm_team_name = ? WHERE id = ?", (norm_key, t_norm, p_id))
            else:
                try:
                    cursor.execute(
                        """
                        INSERT INTO squad_players (team_name, player_name, position, norm_name, norm_team_name)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(team_name, player_name) DO UPDATE SET
                            position = COALESCE(excluded.position, squad_players.position),
                            norm_name = COALESCE(excluded.norm_name, squad_players.norm_name),
                            norm_team_name = COALESCE(excluded.norm_team_name, squad_players.norm_team_name)
                        """,
                        (t_clean, clean, pos_clean, norm_key, t_norm)
                    )
                    new_id = cursor.lastrowid
                    if new_id:
                        retained_ids.add(new_id)
                    added += 1
                except sqlite3.IntegrityError:
                    pass

        # Delete only players who were NOT retained in the new roster
        for p in current_roster:
            if p["id"] not in retained_ids:
                cursor.execute("DELETE FROM squad_players WHERE id = ?", (p["id"],))
                deleted += 1

        _canonicalize_club_player_stats(cursor, t_clean)

    return deleted, added


def remove_player_from_squad(team_name: str, player_name: str) -> bool:
    """Remove a single player from a club's squad."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM squad_players WHERE LOWER(team_name) = LOWER(?) AND LOWER(player_name) = LOWER(?)",
            (team_name.strip(), player_name.strip())
        )
        return cursor.rowcount > 0


def get_missing_squad_players(team_name: str) -> list[str]:
    """Return player names that appear in match_events for a club but are absent from its squad."""
    if not team_name:
        return []
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT me.player_name
            FROM match_events me
            WHERE LOWER(me.team_name) = LOWER(?)
              AND me.player_name IS NOT NULL AND me.player_name != ''
            ORDER BY me.player_name COLLATE NOCASE ASC
        """, (team_name.strip(),))
        event_names = [row["player_name"] for row in cursor.fetchall()]

        roster = _load_club_roster(cursor, team_name)
        missing = []
        for pname in event_names:
            if find_player_in_squad(pname, team_name, conn=conn):
                continue
            # An OCR spelling of a squad player (Emegha for EMEGA) is not a new player.
            if match_roster_name(pname, roster):
                continue
            missing.append(pname)
        return missing


def add_missing_squad_players(team_name: str | None = None) -> int:
    """Add to club squads any players that appear in match_events but are not yet in the squad.

    If team_name is given, only that club is processed; otherwise all clubs are processed.
    Returns the total number of players added.
    """
    added = 0
    with transaction() as conn:
        cursor = conn.cursor()
        if team_name:
            cursor.execute(
                "SELECT DISTINCT me.player_name, me.team_name FROM match_events me WHERE LOWER(me.team_name) = LOWER(?)",
                (team_name.strip(),)
            )
        else:
            cursor.execute(
                "SELECT DISTINCT me.player_name, me.team_name FROM match_events me"
            )
        rows = cursor.fetchall()
        for row in rows:
            pname = row["player_name"]
            tname = row["team_name"]
            if not pname or not tname:
                continue
            existing = find_player_in_squad(pname, tname, conn=conn)
            if existing:
                # Align match_events spelling to canonical squad_players name if they differ
                if existing["player_name"] != pname:
                    cursor.execute(
                        "UPDATE match_events SET player_name = ? WHERE LOWER(team_name) = LOWER(?) AND player_name = ?",
                        (existing["player_name"], tname.strip(), pname)
                    )
                continue
            # A spelling only the fuzzy tier ties to a squad player is still that
            # player: never insert it as a second one. Fuzzy stays read-side, so
            # match_events keep the spelling and the aggregators fold it on read.
            if match_roster_name(pname, _load_club_roster(cursor, tname)):
                continue

            norm_key = normalize_player_name_key(pname)
            t_clean = tname.strip()
            t_norm = normalize_team_name(resolve_team_name(t_clean) or t_clean)
            try:
                cursor.execute(
                    "INSERT INTO squad_players (team_name, player_name, norm_name, norm_team_name) VALUES (?, ?, ?, ?)",
                    (t_clean, pname.strip(), norm_key, t_norm)
                )
                if cursor.rowcount > 0:
                    added += 1
            except sqlite3.IntegrityError:
                pass
            except sqlite3.Error as e:
                logger.warning(f"Failed to add player '{pname}' to {tname}: {e}")
    return added


def _roster_match_tier(name: str, squad_name: str) -> str:
    """Which `match_roster_name` tier tied `name` to `squad_name`: key, alias or fuzzy."""
    if normalize_player_name_key(name) == normalize_player_name_key(squad_name):
        return "key"
    if is_same_footballer(name, squad_name):
        return "alias"
    return "fuzzy"


def plan_player_spelling_merges(team_name: str | None = None, include_fuzzy: bool = True) -> dict:
    """Spellings in match_events / matches.mvp_player that stand for a squad player.

    Returns {"events": [...], "mvp": [...]}; each item carries `old_name`,
    `new_name` (the squad spelling) and `method` (see `_roster_match_tier`).
    Event items are per (team_name, old_name) with `rows` and `total`; MVP items
    are per match. Read-only: `apply_player_spelling_merges` writes the plan.

    `match_roster_name` is a read-side resolver, so this plan is meant to be
    reviewed by a person before it is applied; `include_fuzzy=False` leaves out
    the spellings only its fuzzy tier ties to a squad player.
    """
    events: list[dict] = []
    mvp: list[dict] = []
    with transaction() as conn:
        cursor = conn.cursor()
        rosters: dict[str, list[str]] = {}

        def roster_of(team: str | None) -> list[str]:
            t = (team or "").strip()
            canon = (resolve_team_name(t) or t) if t else ""
            if canon not in rosters:
                rosters[canon] = _load_club_roster(cursor, canon)
            return rosters[canon]

        def squad_name(name: str, team: str | None) -> tuple[str, str] | None:
            hit = match_roster_name(name, roster_of(team))
            if not hit or hit == name:
                return None
            method = _roster_match_tier(name, hit)
            if method == "fuzzy" and not include_fuzzy:
                return None
            return hit, method

        query = """
            SELECT team_name, player_name, COUNT(*) AS rows_n, COALESCE(SUM(count), 0) AS total
            FROM match_events
            WHERE player_name IS NOT NULL AND TRIM(player_name) != ''
              AND team_name IS NOT NULL AND TRIM(team_name) != ''
        """
        params: list = []
        if team_name:
            query += " AND LOWER(team_name) = LOWER(?)"
            params.append(team_name.strip())
        query += " GROUP BY team_name, player_name ORDER BY team_name, player_name"
        cursor.execute(query, tuple(params))
        for row in cursor.fetchall():
            hit = squad_name(row["player_name"], row["team_name"])
            if hit:
                events.append({
                    "team_name": row["team_name"], "old_name": row["player_name"],
                    "new_name": hit[0], "method": hit[1],
                    "rows": row["rows_n"], "total": row["total"],
                })

        query = """
            SELECT id, mvp_player, player1_team, player2_team
            FROM matches
            WHERE mvp_player IS NOT NULL AND TRIM(mvp_player) != ''
        """
        params = []
        if team_name:
            query += " AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?))"
            params += [team_name.strip(), team_name.strip()]
        query += " ORDER BY id"
        cursor.execute(query, tuple(params))
        for row in cursor.fetchall():
            name = row["mvp_player"]
            sides = [s for s in (row["player1_team"], row["player2_team"]) if s]
            # Already a squad spelling of either side: nothing to merge.
            if any(name in roster_of(s) for s in sides):
                continue
            hits = [(s, squad_name(name, s)) for s in sides]
            hits = [(s, h) for s, h in hits if h]
            if len(hits) == 1:
                side, (new_name, method) = hits[0]
                mvp.append({
                    "match_id": row["id"], "team_name": side, "old_name": name,
                    "new_name": new_name, "method": method,
                })
    return {"events": events, "mvp": mvp}


def apply_player_spelling_merges(plan: dict) -> dict:
    """Write a `plan_player_spelling_merges` plan in one transaction.

    Every UPDATE is guarded by the old value, so a row that changed since the
    plan was made is left alone. Returns {"events": n, "mvp": n} rows updated.
    """
    updated = {"events": 0, "mvp": 0}
    with transaction() as conn:
        cursor = conn.cursor()
        for item in plan.get("events", []):
            cursor.execute(
                "UPDATE match_events SET player_name = ? WHERE team_name = ? AND player_name = ?",
                (item["new_name"], item["team_name"], item["old_name"])
            )
            updated["events"] += cursor.rowcount
        for item in plan.get("mvp", []):
            cursor.execute(
                "UPDATE matches SET mvp_player = ? WHERE id = ? AND mvp_player = ?",
                (item["new_name"], item["match_id"], item["old_name"])
            )
            updated["mvp"] += cursor.rowcount
    return updated


def backup_database(target_path: str) -> None:
    """Consistent online copy of the database, WAL included, via SQLite's backup API."""
    src = get_connection()
    dst = sqlite3.connect(target_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def get_club_top_scorers(team_name: str) -> list[dict]:
    """Get top goal scorers for a club across confirmed matches."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT me.player_name, SUM(me.count) as total
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE LOWER(me.team_name) = LOWER(?) AND me.event_type = 'goal' AND m.status = 'confirmed'
            GROUP BY me.player_name
        """, (team_name.strip(),))
        rows = [{"player_name": r["player_name"], "team_name": team_name, "total": r["total"]}
                for r in cursor.fetchall()]
        rows = _fold_player_rows(cursor, rows, ("total",))
        rows.sort(key=lambda r: (-(r["total"] or 0), r["player_name"]))
        return [{"player_name": r["player_name"], "total": r["total"]} for r in rows]


def clear_all_matches(season_id: int | None = None) -> None:
    """Clear all matches from the database for a specific season or all seasons."""
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is not None:
            cursor.execute("DELETE FROM matches WHERE season_id = ?", (season_id,))
            cursor.execute("DELETE FROM rounds WHERE season_id = ?", (season_id,))
        else:
            cursor.execute("DELETE FROM matches")
            cursor.execute("DELETE FROM rounds")


def clear_matches_by_division(division_id: int, season_id: int | None = None) -> None:
    """Clear matches and rounds for a specific division and season."""
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is None:
            act = get_active_season()
            s_id = act["id"] if act else 1
        else:
            s_id = season_id
        cursor.execute("DELETE FROM matches WHERE division_id = ? AND (season_id = ? OR season_id IS NULL)", (division_id, s_id))
        cursor.execute("DELETE FROM rounds WHERE division_id = ? AND (season_id = ? OR season_id IS NULL)", (division_id, s_id))


def batch_insert_matches(fixtures: list[tuple[int, int, int]], division_id: int | None = None, season_id: int | None = None) -> None:
    """Insert a list of matches. fixtures format: (round_number, p1, p2)"""
    valid_fixtures = [f for f in fixtures if f[1] != f[2]]
    if not valid_fixtures:
        return
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    div_id = division_id if division_id is not None else 1
    with transaction() as conn:
        cursor = conn.cursor()
        # Клуб пишем сразу вместе с id. Читатели матчей (`get_match`,
        # `get_matches_by_round`, `get_standings`) связывают матч с users по
        # `LOWER(player1_team) = LOWER(team_name)`, а не по player1_id, поэтому их
        # COALESCE(...,'Команда 1') сам себя не спасает: пустая колонка клуба ломает
        # JOIN, и матч остаётся безымянным — без таблицы, без линии, без ников.
        team_by_id = _team_names_by_telegram_id(
            cursor, {pid for f in valid_fixtures for pid in (f[1], f[2])}
        )
        cursor.executemany(
            "INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team, status, division_id, season_id)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            # div_id, а не division_id: иначе при вызове без дивизиона матчи легли бы
            # с NULL, а туры ниже — в дивизион 1, и сетка туров их уже не нашла бы.
            [
                (f[0], f[1], f[2], team_by_id.get(f[1]), team_by_id.get(f[2]), div_id, s_id)
                for f in valid_fixtures
            ]
        )
        rounds = set([f[0] for f in valid_fixtures])
        cursor.executemany(
            "INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 0, NULL)",
            [(s_id, div_id, r) for r in rounds]
        )


def get_round_info(round_number: int, division_id: int | None = None, season_id: int | None = None) -> dict | None:
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is None:
            act = get_active_season()
            s_id = act["id"] if act else 1
        else:
            s_id = season_id

        if division_id is not None:
            cursor.execute(
                "SELECT round_number, is_open, COALESCE(bets_open, 0) AS bets_open, bets_opened_at, deadline, division_id, season_id, status, closed_at, closed_by FROM rounds WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                (s_id, division_id, round_number)
            )
        else:
            cursor.execute(
                "SELECT round_number, is_open, COALESCE(bets_open, 0) AS bets_open, bets_opened_at, deadline, division_id, season_id, status, closed_at, closed_by FROM rounds WHERE (season_id = ? OR season_id IS NULL) AND round_number = ? LIMIT 1",
                (s_id, round_number)
            )
        row = cursor.fetchone()
        return dict(row) if row else None


def _round_scope_divisions(cursor, round_number: int, season_id: int) -> list[int]:
    """Дивизионы, которых касается операция над туром внутри ОДНОГО сезона.

    Нужна там, где вызывающий не задал дивизион явно — это глобальная
    админ-операция «открыть/закрыть тур N по всей лиге». Вместо одного
    незаскоупленного UPDATE такая операция раскладывается на конкретные
    (season_id, division_id, round_number) и выполняется по каждому из них,
    так что запись всегда идёт в пределах своего scope.

    Учитываются и дивизионы, у которых есть строка тура, и дивизионы, у которых
    есть матчи этого тура (у матча `division_id` может быть NULL — по принятому
    в проекте соглашению это дивизион 1, см. `place_user_bet`).
    """
    divs: set[int] = set()
    cursor.execute(
        "SELECT DISTINCT division_id FROM rounds WHERE season_id = ? AND round_number = ?",
        (season_id, round_number)
    )
    for row in cursor.fetchall():
        if row["division_id"] is not None:
            divs.add(row["division_id"])
    cursor.execute(
        "SELECT DISTINCT COALESCE(division_id, 1) AS div FROM matches "
        "WHERE round_number = ? AND COALESCE(season_id, 1) = ?",
        (round_number, season_id)
    )
    for row in cursor.fetchall():
        divs.add(row["div"])
    return sorted(divs)


def _close_line_scope(cursor, match_ids_sql: str, params: tuple) -> None:
    """Погасить линию по выборке id матчей — во всех трёх схемах разом.

    Правило «закрыть линию = снять legacy-тайл, закрыть рынки и заблокировать
    исходы» живёт здесь в единственном экземпляре: тур лиги и этап кубка передают
    только свою выборку матчей. Рассчитанный рынок ('settled'/'voided') не трогается.

    `match_ids_sql` — служебный SQL-фрагмент (`SELECT id FROM matches WHERE ...`),
    а не пользовательский ввод: подстановка та же, что в `SAFE_COLUMNS` и в
    плейсхолдерах `prune_round_markets`. Фрагмент входит в каждый из трёх запросов
    ровно один раз, поэтому параметры во всех трёх — одни и те же.
    """
    cursor.execute(
        f"UPDATE bet_markets SET is_active = 0 WHERE match_id IN ({match_ids_sql})",
        params
    )
    cursor.execute(
        f"UPDATE markets SET status = 'closed' "
        f"WHERE status IN ('open', 'suspended') AND match_id IN ({match_ids_sql})",
        params
    )
    cursor.execute(
        f"UPDATE market_selections SET status = 'locked' "
        f"WHERE status = 'active' AND market_id IN "
        f"(SELECT id FROM markets WHERE match_id IN ({match_ids_sql}))",
        params
    )


def close_round_betting_line(cursor, round_number: int, division_id: int, season_id: int) -> None:
    """Закрыть линию тура во ВСЕХ схемах разом — строго в пределах одного scope.

    Scope операции — `season_id + division_id + round_number`. Закрытие линии
    Тура 5 Дивизиона 1 Сезона 2026 не должно касаться ни Дивизиона 2 того же
    сезона, ни Тура 5 другого сезона, ни соседнего тура.

    Legacy `bet_markets` не хранит division/season — его единственная связь со
    scope это `match_id`, поэтому он закрывается через подзапрос по `matches`,
    а не по `WHERE tour = ?` (последнее гасило линию во всех дивизионах и всех
    сезонах сразу). Одновременно закрываются реляционные `markets` /
    `market_selections`, из которых берёт коэффициенты Mini App.

    Вызывается ВНУТРИ уже открытой транзакции.
    """
    if division_id is None or season_id is None:
        raise ValueError(
            "close_round_betting_line requires explicit division_id and season_id: "
            "глобальный scope для операций с линией недопустим"
        )

    _close_line_scope(
        cursor,
        "SELECT id FROM matches WHERE round_number = ? "
        "AND COALESCE(division_id, 1) = ? AND COALESCE(season_id, 1) = ?",
        (round_number, division_id, season_id)
    )


def reopen_round_betting_line(cursor, round_number: int, division_id: int, season_id: int) -> None:
    """Вернуть в линию реляционные рынки тура, ранее закрытые `close_round_betting_line`.

    Scope тот же — `season_id + division_id + round_number`: переоткрытие линии
    одного дивизиона не должно открывать линию соседнего.

    Затрагивает только рынки в статусе 'closed' у ещё не сыгранных матчей.
    Рассчитанные ('settled'/'voided') и вручную приостановленные ('suspended')
    рынки не трогаются. Вызывается ВНУТРИ уже открытой транзакции.
    """
    if division_id is None or season_id is None:
        raise ValueError(
            "reopen_round_betting_line requires explicit division_id and season_id: "
            "глобальный scope для операций с линией недопустим"
        )

    scope_params = (round_number, division_id, season_id)

    cursor.execute(
        "UPDATE markets SET status = 'open' "
        "WHERE status = 'closed' "
        "AND match_id IN ("
        "    SELECT id FROM matches WHERE round_number = ? "
        "    AND status IN ('scheduled', 'pending', 'live', 'open') "
        "    AND COALESCE(division_id, 1) = ? AND COALESCE(season_id, 1) = ?"
        ")",
        scope_params
    )
    cursor.execute(
        "UPDATE market_selections SET status = 'active' "
        "WHERE status = 'locked' "
        "AND market_id IN ("
        "    SELECT id FROM markets WHERE status = 'open' AND match_id IN ("
        "        SELECT id FROM matches WHERE round_number = ? "
        "        AND status IN ('scheduled', 'pending', 'live', 'open') "
        "        AND COALESCE(division_id, 1) = ? AND COALESCE(season_id, 1) = ?"
        "    )"
        ")",
        scope_params
    )


def prune_round_markets(
    round_number: int,
    keep_match_ids: list[int] | None,
    division_id: int | None = None,
    season_id: int | None = None,
) -> int:
    """Погасить рынки тура по матчам, не вошедшим в число центральных.

    В линии тура стоит ровно четыре матча (`select_top_round_matches`). Если
    состав центральных пар пересчитался, рынки выпавших матчей должны исчезнуть
    из линии, иначе в ней накапливаются лишние пары.

    Матч, по которому уже принята хоть одна ставка, из линии не выбрасывается
    никогда: купон игрока рассчитывается по `match_id` и без рынка, но гасить
    видимую пару с живыми ставками — значит вводить игрока в заблуждение.

    Возвращает число погашенных legacy-рынков. Scope — `season + division + round`.
    """
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id
    div_id = division_id if division_id is not None else 1

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM matches WHERE round_number = ? "
            "AND COALESCE(division_id, 1) = ? AND COALESCE(season_id, 1) = ?",
            (round_number, div_id, s_id)
        )
        round_match_ids = {int(r["id"]) for r in cursor.fetchall()}
        keep = {int(m) for m in (keep_match_ids or [])}
        stale = round_match_ids - keep
        if not stale:
            return 0

        # Матчи с уже принятыми ставками остаются в линии.
        placeholders = ",".join("?" for _ in stale)
        cursor.execute(
            f"SELECT DISTINCT match_id FROM bet_items WHERE match_id IN ({placeholders})",
            tuple(stale)
        )
        stale -= {int(r["match_id"]) for r in cursor.fetchall()}
        if not stale:
            return 0

        pruned = 0
        for m_id in sorted(stale):
            cursor.execute("UPDATE bet_markets SET is_active = 0 WHERE match_id = ? AND is_active = 1", (m_id,))
            pruned += cursor.rowcount
            cursor.execute(
                "UPDATE markets SET status = 'closed' WHERE status IN ('open', 'suspended') AND match_id = ?",
                (m_id,)
            )
            cursor.execute(
                "UPDATE market_selections SET status = 'locked' WHERE status = 'active' "
                "AND market_id IN (SELECT id FROM markets WHERE match_id = ?)",
                (m_id,)
            )
        return pruned


def _match_line_is_open(cursor, match_id: int) -> bool:
    """Принимает ли матч ставки прямо сейчас — по `evaluate_betting_gate`.

    Линию начавшегося тура (is_open = 1 или bets_open = 0), тура с истёкшим
    дедлайном и сыгранного матча трогать нельзя: её закрыли намеренно, и
    переоткрытый рынок снова дал бы кэшаут по ставкам уже идущего тура.
    """
    cursor.execute(
        "SELECT round_number, division_id, season_id, tournament_type, stage_id "
        "FROM matches WHERE id = ?",
        (match_id,)
    )
    row = cursor.fetchone()
    if row is None:
        return False
    allowed, _reason, _message = evaluate_betting_gate(cursor, row, match_id=match_id)
    return bool(allowed)


def match_line_is_open(match_id: int) -> bool:
    """Публичная обёртка `_match_line_is_open` для сервисного слоя."""
    with transaction() as conn:
        return _match_line_is_open(conn.cursor(), match_id)


BET_SETTLED_EVENT = "BET_SETTLED"


def enqueue_bet_settled_notice(
    cursor, user_id: int, bet_id: int, title: str, body: str, resettle: bool = False
) -> bool:
    """Поставить личное уведомление о расчёте ставки в очередь `notification_events`.

    Вызывается на курсоре транзакции расчёта, поэтому уведомление появляется
    ровно тогда, когда фиксируется выплата, — какой бы путь её ни провёл
    (подтверждение матча, техническое поражение, правка счёта админом,
    фоновый досчёт). Первый расчёт ставки дедуплицируется ключом
    `bet_<id>`; каждый пересчёт получает свой порядковый ключ.

    Не бросает: вставка отсекается, если пользователя нет в `users` (FK) или
    он отключил BET_SETTLED, а любая ошибка только логируется — уведомление
    не должно откатывать расчёт.
    """
    try:
        if resettle:
            cursor.execute(
                "SELECT COUNT(*) FROM notification_events "
                "WHERE user_id = ? AND event_type = ? AND source_event_id LIKE ?",
                (user_id, BET_SETTLED_EVENT, f"bet_{bet_id}_rs%"),
            )
            source_event_id = f"bet_{bet_id}_rs{cursor.fetchone()[0] + 1}"
        else:
            source_event_id = f"bet_{bet_id}"
        cursor.execute(
            """
            INSERT OR IGNORE INTO notification_events
                (user_id, event_type, source_event_id, title, body, priority, status, created_at)
            SELECT ?, ?, ?, ?, ?, 'high', 'pending', datetime('now', '+3 hours')
            WHERE EXISTS (SELECT 1 FROM users WHERE telegram_id = ?)
              AND NOT EXISTS (
                  SELECT 1 FROM user_notification_settings
                  WHERE user_id = ? AND notification_type = ? AND is_enabled = 0
              )
            """,
            (user_id, BET_SETTLED_EVENT, source_event_id, title, body,
             user_id, user_id, BET_SETTLED_EVENT),
        )
        return cursor.rowcount > 0
    except Exception as e:
        logger.warning("Could not enqueue bet notice for bet #%s: %s", bet_id, e)
        return False


def get_bet_legs_for_notice(cursor, bet_id: int) -> list[dict]:
    """События купона с командами и счётом — для текста уведомления о расчёте.

    Читает на курсоре транзакции расчёта, поэтому видит уже проставленные статусы
    событий и счёт. Команды старых матчей без строки в `matches` берутся из
    `bet_markets` подзапросом: JOIN по match_id размножал бы события.
    """
    cursor.execute(
        """
        SELECT bi.outcome_type, bi.odd, bi.status,
               COALESCE(m.player1_team, cs.team1_name,
                        (SELECT bm.team1_name FROM bet_markets bm WHERE bm.match_id = bi.match_id LIMIT 1),
                        'Хозяева') AS team1_name,
               COALESCE(m.player2_team, cs.team2_name,
                        (SELECT bm.team2_name FROM bet_markets bm WHERE bm.match_id = bi.match_id LIMIT 1),
                        'Гости') AS team2_name,
               m.player1_score, m.player2_score,
               mkt.market_key, ms.selection_name
        FROM bet_items bi
        LEFT JOIN matches m ON bi.match_id = m.id
        LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
        LEFT JOIN markets mkt ON bi.market_id = mkt.id
        LEFT JOIN market_selections ms ON bi.selection_id = ms.id
        WHERE bi.bet_id = ?
        ORDER BY bi.id
        """,
        (bet_id,),
    )
    return [dict(r) for r in cursor.fetchall()]


def get_pending_notification_events(limit: int = 25, event_types: tuple | None = None) -> list[dict]:
    """Ожидающие отправки уведомления, важные первыми.

    `event_types` сужает выборку — так очередь доставляет расчёты ставок,
    пока остальные «умные» уведомления выключены флагом.
    """
    query = "SELECT id, user_id, event_type, title, body, link FROM notification_events WHERE status = 'pending'"
    params: list = []
    if event_types:
        query += " AND event_type IN (" + ",".join("?" for _ in event_types) + ")"
        params.extend(event_types)
    query += (" ORDER BY CASE priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2"
              " WHEN 'normal' THEN 3 ELSE 4 END, id ASC LIMIT ?")
    params.append(limit)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def mark_notification_event_failed(event_id: int) -> None:
    """Пометить уведомление неотправляемым (бот заблокирован, чат не найден)."""
    with transaction() as conn:
        conn.cursor().execute("UPDATE notification_events SET status = 'failed' WHERE id = ?", (event_id,))


def reopen_match_markets(match_id: int) -> int:
    """Вернуть в продажу рынки матча, снова попавшего в линию тура.

    `prune_round_markets` закрывает рынки выпавшего из центральных матча, а
    `save_bet_market` при его возвращении включает только legacy-строку линии:
    реляционные рынки так и остаются `closed`, и ставку на видимый в линии
    коэффициент отклоняет `place_user_bet`. Открываем их обратно — но только
    пока тур матча принимает ставки: иначе воскрес бы рынок, закрытый стартом
    тура или расчётом.

    Возвращает число переоткрытых рынков.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if not _match_line_is_open(cursor, match_id):
            return 0
        cursor.execute(
            "UPDATE markets SET status = 'open' WHERE match_id = ? AND status = 'closed'",
            (match_id,)
        )
        reopened = cursor.rowcount
        cursor.execute(
            "UPDATE market_selections SET status = 'active' WHERE status = 'locked' "
            "AND market_id IN (SELECT id FROM markets WHERE match_id = ? AND status = 'open')",
            (match_id,)
        )
        return reopened


def _evaluate_gate_row(
    cursor,
    gate_row,
    scope_label: str,
    match_id: int | None = None,
) -> tuple[bool, str | None, str | None]:
    """ЕДИНСТВЕННАЯ реализация правила приёма прогнозов — по строке-разрешению.

    `gate_row` — это `rounds` одного тура или `cup_stages` одного этапа. Колонки в
    `cup_stages` названы так же, как в `rounds`, именно ради этого метода: предикат
    «линия открыта, игра ещё не началась, дедлайн не истёк, матч не сыгран» у лиги и
    кубка один, и второй копии у него быть не может (`FIX_01`, `FIX_03` и `FIX_04`
    выросли как раз из проверок, которые разъехались между слоями).

    `scope_label` — только то, как назвать скоуп в тексте для человека («Тур 5»,
    «Этап 1/64»); на решение он не влияет.

    Возвращает (allowed, reason, message). Вызывается внутри транзакции.
    """
    if gate_row["is_open"]:
        return False, "ROUND_STARTED", f"{scope_label} уже открыт — приём прогнозов закрыт."

    if not gate_row["bets_open"]:
        return False, "LINE_CLOSED", f"Приём прогнозов на {scope_label} закрыт."

    if gate_row["deadline"]:
        dl_dt = _parse_round_deadline(gate_row["deadline"])
        if dl_dt and now_msk() > dl_dt:
            return False, "DEADLINE_PASSED", f"Дедлайн для прогнозов на {scope_label} истек."

    # Принцип pre-match: на сыгранный матч ставку не принять даже при открытой
    # линии тура (матч мог быть подтверждён досрочно или получить ТП/ТН).
    if match_id is not None:
        cursor.execute("SELECT status FROM matches WHERE id = ? LIMIT 1", (match_id,))
        m_row = cursor.fetchone()
        if not m_row:
            return False, "MATCH_NOT_FOUND", f"Матч #{match_id} не найден."
        if m_row["status"] in ("confirmed", "completed"):
            return False, "MATCH_FINISHED", f"Матч #{match_id} уже сыгран — приём прогнозов закрыт."

    return True, None, None


def evaluate_round_betting_gate(
    cursor,
    round_number: int | None,
    division_id: int | None,
    season_id: int | None = None,
    match_id: int | None = None,
) -> tuple[bool, str | None, str | None]:
    """Серверное правило приёма ставок на тур лиги.

    Ставка разрешена ТОЛЬКО когда тур ещё не открыт для игры, а линия открыта:

        rounds.is_open = 0  AND  rounds.bets_open = 1

    Проверяется строка ровно того тура, которому принадлежит матч:
    `season_id + division_id + round_number`. Совпадения только по номеру тура
    недостаточно — Тур 5 Дивизиона 2 не может разрешить ставку в Дивизионе 1,
    и Тур 5 прошлого сезона не может разрешить ставку в текущем.
    `UNIQUE(season_id, division_id, round_number)` гарантирует, что такой строки
    не больше одной, поэтому выборка точная, без ORDER BY и без подстановок.

    Любое другое состояние — отказ. Отсутствие строки нужного тура — тоже отказ:
    разрешающего fallback здесь нет и быть не должно. Сам предикат живёт в
    `_evaluate_gate_row` и общий у лиги с кубком.

    Возвращает (allowed, reason, message). Вызывается внутри транзакции.
    Используется и `place_user_bet`, и `RiskEngine`, чтобы Telegram, Mini App
    и REST API проверялись одним и тем же инвариантом.
    """
    if not round_number:
        return False, "ROUND_UNKNOWN", "Матч не привязан к туру — приём прогнозов недоступен."

    # У матча `division_id` может быть NULL (legacy-строки) — по принятому в
    # проекте соглашению это дивизион 1. `season_id` у матча NOT NULL, но если
    # scope всё же не задан, берём активный сезон, а не «любой».
    div_id = division_id if division_id is not None else 1
    if season_id is None:
        act = get_active_season()
        season_id = act["id"] if act else 1

    cursor.execute(
        "SELECT is_open, COALESCE(bets_open, 0) AS bets_open, deadline FROM rounds "
        "WHERE round_number = ? AND division_id = ? AND season_id = ? "
        "LIMIT 1",
        (round_number, div_id, season_id)
    )
    r_row = cursor.fetchone()
    if not r_row:
        return False, "ROUND_NOT_FOUND", f"Тур {round_number} не найден — приём прогнозов недоступен."

    return _evaluate_gate_row(cursor, r_row, f"Тур {round_number}", match_id=match_id)


def evaluate_cup_stage_gate(
    cursor,
    stage_id: int | None,
    match_id: int | None = None,
) -> tuple[bool, str | None, str | None]:
    """Серверное правило приёма ставок на этап кубка — тот же предикат, другая строка.

    Отличается только тем, где искать разрешение: `cup_stages.id` вместо
    (`season_id`, `division_id`, `round_number`). У этапа может не быть дедлайна —
    тогда проверка срока просто пропускается, как и в лиге: «открыть кубок» закрывает
    линию раньше любого дедлайна.
    """
    if not stage_id:
        return False, "STAGE_UNKNOWN", "Матч не привязан к этапу кубка — приём прогнозов недоступен."

    cursor.execute(
        "SELECT stage, is_open, COALESCE(bets_open, 0) AS bets_open, deadline "
        "FROM cup_stages WHERE id = ? LIMIT 1",
        (stage_id,)
    )
    st_row = cursor.fetchone()
    if not st_row:
        return False, "STAGE_NOT_FOUND", f"Этап кубка #{stage_id} не найден — приём прогнозов недоступен."

    return _evaluate_gate_row(cursor, st_row, f"Этап {cup_stage_title(st_row['stage'])}", match_id=match_id)


def _row_col(row, name, default=None):
    """Значение колонки из `sqlite3.Row` или dict; `default` — если колонки нет.

    Строку матча передают разные слои: `place_user_bet` читает `SELECT *`,
    RiskEngine — узкий `SELECT`. Отсутствующая колонка не должна становиться
    KeyError внутри проверки приёма ставки.
    """
    if row is None:
        return default
    try:
        keys = row.keys()
    except AttributeError:
        return row.get(name, default) if hasattr(row, "get") else default
    return row[name] if name in keys else default


def match_is_cup(match_row) -> bool:
    """Кубковая ли это строка `matches`: игра серии или её заголовок.

    Строке без `tournament_type` кубок не сочувствует: отсутствие поля читается
    как «лига» — то же соглашение, что в `evaluate_betting_gate`.
    """
    return (_row_col(match_row, "tournament_type") or "league") == "cup"


def cup_outcome_missing_error(match_row, out_type: str) -> dict | None:
    """Отказ «такого исхода в кубковой линии нет» — или None, если матч лиговый.

    Кубку нечего ловить в legacy-`bet_markets`: тайлов этапу не заводят
    (`get_cup_stage_line`), поэтому отсутствующий реляционный исход означает
    ровно одно — рынка не существует, а не «рынок закрыли». Ничьей в кубке нет,
    поэтому `x`/`1X`/`X2`/`12` отсутствуют по построению росписи, и на заголовке
    серии нет ни индивидуальных тоталов, ни форы. Отличать это от технической
    недоступности нужно и игроку, и Mini App: `x` в экспрессе лиги — ошибка
    кэша, `x` в кубке — исходы такого просто не бывает.
    """
    if not match_is_cup(match_row):
        return None
    return {
        "error": "MARKET_NOT_OFFERED",
        "match_id": _row_col(match_row, "id"),
        "outcome": out_type,
        "message": f"Исход '{out_type}' в линии общего кубка не разыгрывается.",
    }


def evaluate_betting_gate(cursor, match_row, match_id: int | None = None) -> tuple[bool, str | None, str | None]:
    """Где искать разрешающую строку для этого матча: тур лиги или этап кубка.

    Кубковая ветка включается только когда матч явно кубковый И привязан к этапу.
    Кубковый матч без `stage_id` уходит в лиговый путь и получает отказ там
    (`ROUND_NOT_FOUND`) — отсутствующая привязка не должна становиться разрешением,
    поэтому fallback намеренно один и он отказывающий.

    `match_row` — строка `matches` (sqlite3.Row или dict).
    """
    if match_is_cup(match_row) and _row_col(match_row, "stage_id"):
        return evaluate_cup_stage_gate(cursor, _row_col(match_row, "stage_id"), match_id=match_id)

    return evaluate_round_betting_gate(
        cursor,
        _row_col(match_row, "round_number"),
        _row_col(match_row, "division_id"),
        _row_col(match_row, "season_id"),
        match_id=match_id,
    )


def find_self_participation_match(cursor, user_id: int | None, selections: list[dict]) -> int | None:
    """322-защита: найти в купоне матч, в котором сам ставящий является участником.

    Игрок не имеет права ставить ни на один исход (П1, X, П2, тоталы, ОЗ) матча,
    где он играет сам. Перебираются ВСЕ исходы купона: одного попадания
    достаточно, чтобы отклонить весь экспресс целиком.

    Участие определяется тремя способами, и достаточно любого:
      * `matches.player1_id` / `player2_id` — Telegram ID (`users.telegram_id`);
      * `matches.player1_team` / `player2_team` — имя клуба, если ID в строке матча
        не заполнен (legacy-расписания). Имена клубов глобально уникальны
        (`idx_users_team_name_unique`), поэтому такое сопоставление однозначно;
      * строка-заголовок серии общего кубка: у неё ни ID, ни имён клубов нет
        (`create_cup_series_header`), иначе она попал бы в «Мои матчи» и в приёмку
        отчётов. Ставящий участвует в серии, если его клуб стоит в её паре
        (`cup_series.team1_name` / `team2_name`).

    Возвращает id первого найденного матча или None. Вызывается внутри транзакции;
    дивизион матча и дивизион игрока намеренно не сравниваются — ставить на чужие
    дивизионы разрешено.
    """
    if not user_id or not selections:
        return None

    for s in selections:
        if not isinstance(s, dict):
            continue
        m_id = s.get("match_id")
        if not m_id:
            continue
        cursor.execute(
            """
            SELECT 1
            FROM matches m
            LEFT JOIN users u ON u.telegram_id = ?
            WHERE m.id = ?
              AND (
                  m.player1_id = ?
                  OR m.player2_id = ?
                  OR (
                      u.team_name IS NOT NULL AND TRIM(u.team_name) != ''
                      AND (
                          LOWER(TRIM(m.player1_team)) = LOWER(TRIM(u.team_name))
                          OR LOWER(TRIM(m.player2_team)) = LOWER(TRIM(u.team_name))
                      )
                  )
                  OR (
                      m.is_series_header = 1
                      AND u.team_name IS NOT NULL AND TRIM(u.team_name) != ''
                      AND EXISTS (
                          SELECT 1 FROM cup_series cs
                          WHERE cs.id = m.cup_series_id
                            AND (
                                LOWER(TRIM(cs.team1_name)) = LOWER(TRIM(u.team_name))
                                OR LOWER(TRIM(cs.team2_name)) = LOWER(TRIM(u.team_name))
                            )
                      )
                  )
              )
            LIMIT 1
            """,
            (user_id, m_id, user_id, user_id)
        )
        if cursor.fetchone():
            return int(m_id)

    return None


def update_round_status(round_number: int, is_open: bool, deadline: str | None = None, division_id: int | None = None, season_id: int | None = None, closed_by: int | None = None) -> None:
    """Open/close a round. When opening without an explicit deadline, any stale
    stored deadline is cleared so it cannot instantly mark matches as overdue.
    Whenever the deadline is (re)set, per-round reminder flags are reset so
    the 24h/6h/1h pipeline works for the new deadline window.

    Открытие тура без расписания запрещено: если в `matches` нет ни одного матча
    для (season, division, round), бросается `RoundScheduleMissingError` и в БД
    не пишется ничего — ни строки тура, ни `is_open = 1`. Закрытие тура
    (`is_open=False`) проверке не подлежит: закрыть пустой тур всегда можно.

    Низкоуровневая операция: дедлайн здесь не проверяется и закрытый тур
    открывается повторно без вопросов. Хендлеры идут через
    `validate_round_deadline`, `close_round` и `reopen_round`, где живут
    правила регламента. Закрытие тура переводит его несыгранные матчи в долг
    (`match_debts`), открытие и смена дедлайна снимают долги, по которым ещё
    не было ни вердикта, ни награды."""
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    if is_open:
        act = get_season(s_id)
        if act and act.get("status") not in ("active", None):
            raise ValueError(f"Cannot open round in season #{s_id} with status '{act.get('status')}'")

    # Смена состояния тура сериализуется с приёмом ставок: пока идёт place_user_bet,
    # тур не может открыться «в середине» проверки, и наоборот.
    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()
        if is_open:
            # Расписание — предусловие открытия. Проверяется внутри той же
            # транзакции и до первой записи, чтобы «фантомный» открытый тур
            # без пар не мог появиться даже частично.
            if _count_round_matches(cursor, round_number, division_id, s_id) == 0:
                raise RoundScheduleMissingError(round_number, division_id, s_id)

            # Лимит одновременно активных туров — второе предусловие открытия.
            # Уже активный тур собственный слот повторно не занимает, поэтому
            # смена дедлайна открытого тура сюда не упирается.
            if division_id is not None:
                _assert_rounds_within_limit(cursor, [round_number], division_id, s_id)
            else:
                for scope_div_id in _round_scope_divisions(cursor, round_number, s_id):
                    _assert_rounds_within_limit(cursor, [round_number], scope_div_id, s_id)

            if division_id is not None:
                cursor.execute(
                    "INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 0, NULL)",
                    (s_id, division_id, round_number)
                )
            else:
                cursor.execute(
                    "INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, 1, ?, 0, NULL)",
                    (s_id, round_number)
                )

        if division_id is not None:
            if deadline is not None:
                cursor.execute(
                    "UPDATE rounds SET is_open = ?, deadline = ? WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                    (1 if is_open else 0, deadline, s_id, division_id, round_number)
                )
            elif is_open:
                cursor.execute(
                    "UPDATE rounds SET is_open = ?, deadline = NULL WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                    (1, s_id, division_id, round_number)
                )
            else:
                cursor.execute(
                    "UPDATE rounds SET is_open = ? WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                    (0, s_id, division_id, round_number)
                )

            # Открытие тура для игры ЗАКРЫВАЕТ приём прогнозов на него; закрытие
            # тура линию тоже не открывает. Состояние is_open=1 AND bets_open=1
            # для production-тура недостижимо.
            cursor.execute(
                "UPDATE rounds SET bets_open = 0, bets_opened_at = NULL WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                (s_id, division_id, round_number)
            )
            close_round_betting_line(cursor, round_number, division_id=division_id, season_id=s_id)

            if deadline is not None:
                cursor.execute(
                    "DELETE FROM round_reminders WHERE round_number = ? AND (division_id = ? OR division_id IS NULL)",
                    (round_number, division_id)
                )
        else:
            if deadline is not None:
                cursor.execute(
                    "UPDATE rounds SET is_open = ?, deadline = ? WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                    (1 if is_open else 0, deadline, s_id, round_number)
                )
            elif is_open:
                # Re-opening without a new deadline: drop the stale one
                cursor.execute(
                    "UPDATE rounds SET is_open = ?, deadline = NULL WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                    (1, s_id, round_number)
                )
            else:
                cursor.execute(
                    "UPDATE rounds SET is_open = ? WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                    (0, s_id, round_number)
                )

            # 🎰 Открытие тура для игры закрывает его линию (и закрытие — тоже).
            cursor.execute(
                "UPDATE rounds SET bets_open = 0, bets_opened_at = NULL WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                (s_id, round_number)
            )
            # Дивизион не задан — это глобальная админ-операция по всей лиге.
            # Линия при этом закрывается не одним общим UPDATE, а отдельно по
            # каждому конкретному (season, division, round): сезон s_id чужие
            # сезоны не затрагивает ни при каких обстоятельствах.
            for scope_div_id in _round_scope_divisions(cursor, round_number, s_id):
                close_round_betting_line(cursor, round_number, division_id=scope_div_id, season_id=s_id)

            if deadline is not None:
                cursor.execute("DELETE FROM round_reminders WHERE round_number = ?", (round_number,))

        now = now_msk()
        scope = [division_id] if division_id is not None else _round_scope_divisions(cursor, round_number, s_id)
        for scope_div_id in scope:
            if is_open:
                _after_round_opened(cursor, round_number, scope_div_id, s_id, deadline, now)
            else:
                _mark_round_closed(cursor, round_number, scope_div_id, s_id, closed_by, now)

    if is_open:
        # 🎰 Линия тура N здесь НЕ генерируется: открытие тура для игры её закрывает.
        # Ставки на тур принимаются заранее — до его открытия (см. set_round_bets_open).
        #
        # 🎰 Парный цикл «два через два»: как только туры открыты для игры,
        # линия автоматически уходит на два следующих тура.
        return advance_betting_line_pair(division_id=division_id, season_id=s_id)
    return []


def set_round_bets_open(
    round_number: int,
    bets_open: bool,
    division_id: int | None = None,
    season_id: int | None = None,
) -> bool:
    """Открыть/закрыть приём прогнозов на тур независимо от `rounds.is_open`.

    Позволяет выставить линию заранее — до того, как тур открыт для игры.
    При открытии сразу генерируются рынки. Возвращает False, если у тура нет
    матчей (открывать нечего) или сезон не активен.
    """
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    if bets_open:
        season = get_season(s_id)
        if season and season.get("status") not in ("active", None):
            return False

    # Сериализуется с приёмом ставок — см. update_round_status.
    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()

        if bets_open:
            # Тур, уже открытый для игры, в линию не возвращается:
            # состояние is_open = 1 AND bets_open = 1 недопустимо.
            cursor.execute(
                "SELECT 1 FROM rounds WHERE round_number = ? AND is_open = 1 "
                "AND (? IS NULL OR division_id = ?) AND (season_id = ? OR season_id IS NULL) LIMIT 1",
                (round_number, division_id, division_id, s_id)
            )
            if cursor.fetchone():
                return False

            # Нет матчей — нечего выставлять в линию. Матчи считаются строго
            # в пределах scope: у матча division_id = NULL означает дивизион 1
            # (то же соглашение, что в place_user_bet и close_round_betting_line).
            if _count_round_matches(cursor, round_number, division_id, s_id) == 0:
                return False

            cursor.execute(
                "INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline) VALUES (?, ?, ?, 0, NULL)",
                (s_id, division_id if division_id is not None else 1, round_number)
            )

        opened_at = now_msk_str() if bets_open else None
        if division_id is not None:
            cursor.execute(
                "UPDATE rounds SET bets_open = ?, bets_opened_at = ? WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
                (1 if bets_open else 0, opened_at, s_id, division_id, round_number)
            )
        else:
            cursor.execute(
                "UPDATE rounds SET bets_open = ?, bets_opened_at = ? WHERE (season_id = ? OR season_id IS NULL) AND round_number = ?",
                (1 if bets_open else 0, opened_at, s_id, round_number)
            )
        changed = cursor.rowcount

        # Синхронизация линии всегда идёт по конкретному (season, division, round).
        # Если дивизион не задан (глобальная админ-операция), она раскладывается
        # на каждый затронутый дивизион ЭТОГО сезона — чужие сезоны не трогаются.
        scope_divs = [division_id] if division_id is not None else _round_scope_divisions(cursor, round_number, s_id)

        for scope_div_id in scope_divs:
            if not bets_open:
                # Закрываем линию в ОБЕИХ схемах: legacy bet_markets (Telegram)
                # и реляционные markets/market_selections (Mini App).
                close_round_betting_line(cursor, round_number, division_id=scope_div_id, season_id=s_id)
            else:
                # Повторное открытие линии: рынки, закрытые ранее, возвращаются в игру.
                # Без этого reopen оставил бы markets='closed'/selections='locked',
                # и линия была бы видна, но неставима.
                reopen_round_betting_line(cursor, round_number, division_id=scope_div_id, season_id=s_id)

    if changed == 0:
        return False

    if bets_open:
        try:
            from services.betting_engine import generate_round_markets
            generate_round_markets(round_number, division_id=division_id, season_id=s_id)
        except Exception as e:
            logger.exception(f"Error generating early betting line for round {round_number}: {e}")

    return True


# Сколько туров держится в линии одновременно — парный цикл «два через два».
LINE_PAIR_SIZE = 2


def advance_betting_line_pair(division_id: int | None = None, season_id: int | None = None) -> list[int]:
    """Сдвинуть линию БК на следующую пару туров (автопилот «два через два»).

    Линия всегда стоит на двух ближайших несыгранных турах. Как только туры
    открываются для игры, их приём прогнозов закрывается, а линия автоматически
    переезжает на два тура после самого позднего открытого: R_max+1 и R_max+2.

    Туры без расписания пропускаются, поэтому в конце сезона цикл просто
    затухает, ничего не ломая. Возвращает список туров, на которые линия
    действительно встала.
    """
    if season_id is None:
        act = get_active_season()
        s_id = act["id"] if act else 1
    else:
        s_id = season_id

    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute(
                "SELECT MAX(round_number) AS r FROM rounds WHERE is_open = 1 "
                "AND division_id = ? AND (season_id = ? OR season_id IS NULL)",
                (division_id, s_id)
            )
        else:
            cursor.execute(
                "SELECT MAX(round_number) AS r FROM rounds WHERE is_open = 1 "
                "AND (season_id = ? OR season_id IS NULL)",
                (s_id,)
            )
        row = cursor.fetchone()
        max_open = row["r"] if row and row["r"] is not None else None

    if max_open is None:
        return []

    advanced: list[int] = []
    for offset in range(1, LINE_PAIR_SIZE + 1):
        next_round = int(max_open) + offset
        try:
            if set_round_bets_open(next_round, True, division_id=division_id, season_id=s_id):
                advanced.append(next_round)
        except Exception as e:
            logger.warning(f"Could not pre-open betting line for round {next_round}: {e}")

    if advanced:
        logger.info(
            f"🎰 Betting line advanced to rounds {advanced} "
            f"(division={division_id}, season={s_id})"
        )
    return advanced


def get_all_rounds() -> list[int]:
    """Get a list of all round numbers present in the database for the active season / main league."""
    with transaction() as conn:
        cursor = conn.cursor()
        act = get_active_season()
        s_id = act["id"] if act else 1
        cursor.execute("""
            SELECT DISTINCT round_number 
            FROM matches 
            WHERE round_number > 0 
              AND (season_id = ? OR season_id IS NULL) 
              AND (division_id = 1 OR division_id IS NULL)
            ORDER BY round_number ASC
        """, (s_id,))
        rows = cursor.fetchall()
        if rows:
            return [row["round_number"] for row in rows]
        cursor.execute("SELECT DISTINCT round_number FROM matches WHERE round_number > 0 ORDER BY round_number ASC")
        return [row["round_number"] for row in cursor.fetchall()]

def get_club_top_assisters(team_name: str) -> list[dict]:
    """Get top assist providers for a club across confirmed matches."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT me.player_name, SUM(me.count) as total
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE LOWER(me.team_name) = LOWER(?) AND me.event_type = 'assist' AND m.status = 'confirmed'
            GROUP BY me.player_name
        """, (team_name.strip(),))
        rows = [{"player_name": r["player_name"], "team_name": team_name, "total": r["total"]}
                for r in cursor.fetchall()]
        rows = _fold_player_rows(cursor, rows, ("total",))
        rows.sort(key=lambda r: (-(r["total"] or 0), r["player_name"]))
        return [{"player_name": r["player_name"], "total": r["total"]} for r in rows]


def get_unplayed_matches_in_round(round_number: int) -> list[dict]:
    """Get all unplayed (pending) matches for a specific round with user details."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.id, m.round_number,
                   u1.telegram_id AS player1_id, u1.username as p1_username, u1.team_name as p1_team,
                   u2.telegram_id AS player2_id, u2.username as p2_username, u2.team_name as p2_team
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.round_number = ? AND m.status = 'pending'
            ORDER BY m.id ASC
        """, (round_number,))
        return [dict(row) for row in cursor.fetchall()]

def get_top_scorers(limit: int = 20, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Get top goalscorers in the league aggregated from match_events (strictly confirmed league matches, round_number > 0)."""
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        query = """
            SELECT me.player_name, me.team_name, SUM(me.count) AS total_goals
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE me.event_type = 'goal'
              AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND m.round_number > 0
              AND m.status = 'confirmed'
              AND (m.season_id = ? OR m.season_id IS NULL)
        """
        params = [target_season_id]
        if division_id is not None:
            query += " AND m.division_id = ?"
            params.append(division_id)
        query += """
            GROUP BY me.player_name, me.team_name
        """
        cursor.execute(query, tuple(params))
        rows = _fold_player_rows(cursor, cursor.fetchall(), ("total_goals",))
        rows.sort(key=lambda r: (-(r["total_goals"] or 0), r["player_name"]))
        return rows[:limit]

def get_top_assists(limit: int = 20, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Get top assist providers in the league aggregated from match_events (strictly confirmed league matches, round_number > 0)."""
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        query = """
            SELECT me.player_name, me.team_name, SUM(me.count) AS total_assists
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE me.event_type = 'assist'
              AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND m.round_number > 0
              AND m.status = 'confirmed'
              AND (m.season_id = ? OR m.season_id IS NULL)
        """
        params = [target_season_id]
        if division_id is not None:
            query += " AND m.division_id = ?"
            params.append(division_id)
        query += """
            GROUP BY me.player_name, me.team_name
        """
        cursor.execute(query, tuple(params))
        rows = _fold_player_rows(cursor, cursor.fetchall(), ("total_assists",))
        rows.sort(key=lambda r: (-(r["total_assists"] or 0), r["player_name"]))
        return rows[:limit]


def get_top_mvps(division_id: int | None = None, season_id: int | None = None, limit: int = 15) -> list[dict]:
    """Игроки с наибольшим числом наград «Игрок матча» (золотая корона на скриншоте).

    Возвращает [{"player_name": str, "team_name": str, "mvp_count": int}, ...].

    В `matches.mvp_player` лежит только имя, без ссылки на клуб, поэтому клуб
    восстанавливается детерминированно: сперва по событиям того же матча
    (`match_events.team_name` — там игрок уже привязан к стороне), затем по
    заявленному составу (`squad_players`). Не нашлось ни там, ни там — клуб
    отдаётся пустой строкой, но награда не теряется.

    Если точного совпадения нет (корона на «Emegha», в составе «EMEGA»), имя
    сверяется с составами обоих клубов матча через `match_roster_name`; клуб
    берётся, только когда подошёл ровно один из них.

    Написания одного игрока склеиваются по паре (клуб, игрок) через
    `_fold_player_rows`, а награда с нераспознанным клубом присоединяется к
    единственной строке с тем же ключом игрока — иначе один игрок разъехался бы
    на две строки. Сортировка и LIMIT делаются уже после склейки.
    """
    limit = max(1, int(limit))
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        query = """
            SELECT player_name, team_name, player1_team, player2_team
            FROM (
                SELECT
                    TRIM(m.mvp_player) AS player_name,
                    m.player1_team, m.player2_team,
                    COALESCE(
                        (SELECT me.team_name FROM match_events me
                          WHERE me.match_id = m.id
                            AND LOWER(TRIM(me.player_name)) = LOWER(TRIM(m.mvp_player))
                          LIMIT 1),
                        (SELECT sp.team_name FROM squad_players sp
                          WHERE LOWER(TRIM(sp.player_name)) = LOWER(TRIM(m.mvp_player))
                          LIMIT 1),
                        ''
                    ) AS team_name
                FROM matches m
                WHERE m.status = 'confirmed'
                  AND m.mvp_player IS NOT NULL
                  AND TRIM(m.mvp_player) <> ''
                  AND (m.season_id = ? OR m.season_id IS NULL)
        """
        params = [target_season_id]
        if division_id is not None:
            query += " AND m.division_id = ?"
            params.append(division_id)
        query += """
            )
        """
        cursor.execute(query, tuple(params))
        awards = []
        for row in cursor.fetchall():
            name, team = row["player_name"], row["team_name"] or ""
            if not team:
                hits = []
                for side in (row["player1_team"], row["player2_team"]):
                    hit = match_roster_name(name, _load_club_roster(cursor, side))
                    if hit:
                        hits.append((side, hit))
                if len(hits) == 1:
                    team, name = hits[0]
            awards.append({"player_name": name, "team_name": team, "mvp_count": 1})

        with_club = [a for a in awards if a["team_name"]]
        rows = _fold_player_rows(cursor, with_club, ("mvp_count",))
        by_player: dict[str, list[dict]] = {}
        for r in rows:
            key = normalize_player_name_key(r["player_name"]) or r["player_name"].lower()
            by_player.setdefault(key, []).append(r)
        clubless: dict[str, dict] = {}
        for a in awards:
            if a["team_name"]:
                continue
            key = normalize_player_name_key(a["player_name"]) or a["player_name"].lower()
            owners = by_player.get(key, [])
            if len(owners) == 1:
                owners[0]["mvp_count"] += 1
            elif key in clubless:
                clubless[key]["mvp_count"] += 1
            else:
                clubless[key] = a
                rows.append(a)
        rows.sort(key=lambda r: (-r["mvp_count"], r["player_name"]))
        return rows[:limit]


def get_round_player_stats(round_number: int, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Goals and assists per player within a single round (confirmed league matches only).

    Powers the "player of the round" block of the round digest. Sorted by
    goals + assists, then goals, so the top row is the standout performer.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        query = """
            SELECT
                me.player_name,
                me.team_name,
                SUM(CASE WHEN me.event_type = 'goal' THEN me.count ELSE 0 END) AS goals,
                SUM(CASE WHEN me.event_type = 'assist' THEN me.count ELSE 0 END) AS assists
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND m.round_number = ?
              AND m.status = 'confirmed'
              AND (m.season_id = ? OR m.season_id IS NULL)
        """
        params = [round_number, target_season_id]
        if division_id is not None:
            query += " AND m.division_id = ?"
            params.append(division_id)
        query += """
            GROUP BY me.player_name, me.team_name
        """
        cursor.execute(query, tuple(params))
        rows = _fold_player_rows(cursor, cursor.fetchall(), ("goals", "assists"))
        rows.sort(key=lambda r: (-((r["goals"] or 0) + (r["assists"] or 0)), -(r["goals"] or 0), r["player_name"]))
        return rows


# ─── Символическая сборная (TOTW) ─────────────────────────────────────────────

TOTW_BLOCK_SIZE = 5
TOTW_STARTERS = 11


def _round_completion(cursor: sqlite3.Cursor, division_id: int, season_id: int) -> dict[int, tuple[int, int]]:
    """{round_number: (matches_total, matches_confirmed)} over the division's league matches.

    The same rule as `get_rounds_pending_digest`: cancelled matches do not
    count, technical results are confirmed ones.
    """
    cursor.execute("""
        SELECT m.round_number,
               COUNT(m.id) AS matches_total,
               SUM(CASE WHEN m.status = 'confirmed' THEN 1 ELSE 0 END) AS matches_confirmed
        FROM matches m
        WHERE m.division_id = ?
          AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
          AND (m.season_id = ? OR m.season_id IS NULL)
          AND m.round_number > 0
          AND m.status != 'cancelled'
        GROUP BY m.round_number
    """, (division_id, season_id))
    return {
        int(r["round_number"]): (int(r["matches_total"] or 0), int(r["matches_confirmed"] or 0))
        for r in cursor.fetchall()
    }


def _is_range_complete(completion: dict[int, tuple[int, int]], start_round: int, end_round: int) -> bool:
    for rn in range(start_round, end_round + 1):
        total, confirmed = completion.get(rn, (0, 0))
        if total == 0 or confirmed != total:
            return False
    return True


def is_round_range_completed(start_round: int, end_round: int, division_id: int, season_id: int | None = None) -> bool:
    """Whether every league match of rounds start..end of the division is confirmed.

    A round with no matches at all is not complete: a block with a gap in it
    is not a block that was played. A debt keeps its match `pending`, so an
    open debt blocks the range until it is played or judged.
    """
    if start_round < 1 or end_round < start_round:
        return False
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1
        return _is_range_complete(_round_completion(cursor, division_id, target_season_id), start_round, end_round)


def get_completed_totw_blocks_pending_publication(
    division_id: int | None = None,
    season_id: int | None = None,
    block_size: int = TOTW_BLOCK_SIZE,
) -> list[dict]:
    """Fully played blocks of `block_size` rounds whose TOTW has not been posted.

    Blocks are 1–5, 6–10, …; the publication marker is a `round_content_posts`
    row with content_type 'totw' at the block's END round. Returns
    [{"division_id", "season_id", "start_round", "end_round"}, ...].
    """
    block_size = max(1, int(block_size))
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        if division_id is not None:
            division_ids = [division_id]
        else:
            cursor.execute("""
                SELECT DISTINCT division_id FROM matches
                WHERE division_id IS NOT NULL
                  AND (tournament_type IS NULL OR tournament_type = 'league')
                  AND (season_id = ? OR season_id IS NULL)
                ORDER BY division_id
            """, (target_season_id,))
            division_ids = [r["division_id"] for r in cursor.fetchall()]

        pending: list[dict] = []
        for div_id in division_ids:
            completion = _round_completion(cursor, div_id, target_season_id)
            if not completion:
                continue
            cursor.execute(
                "SELECT round_number FROM round_content_posts WHERE division_id = ? AND content_type = 'totw'",
                (div_id,)
            )
            posted = {int(r["round_number"]) for r in cursor.fetchall()}
            last_round = max(completion)
            for start in range(1, last_round + 1, block_size):
                end = start + block_size - 1
                if end > last_round or end in posted:
                    continue
                if _is_range_complete(completion, start, end):
                    pending.append({
                        "division_id": div_id,
                        "season_id": target_season_id,
                        "start_round": start,
                        "end_round": end,
                    })
        return pending


def get_completed_totw_blocks(
    division_id: int,
    season_id: int | None = None,
    block_size: int = TOTW_BLOCK_SIZE,
) -> list[tuple[int, int]]:
    """[(start_round, end_round)] of the division's fully played blocks, posted or not."""
    block_size = max(1, int(block_size))
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1
        completion = _round_completion(cursor, division_id, target_season_id)
        if not completion:
            return []
        last_round = max(completion)
        return [
            (start, start + block_size - 1)
            for start in range(1, last_round - block_size + 2, block_size)
            if _is_range_complete(completion, start, start + block_size - 1)
        ]


def get_last_completed_round(division_id: int, season_id: int | None = None) -> int | None:
    """Highest round of the division whose every league match is confirmed."""
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1
        completion = _round_completion(cursor, division_id, target_season_id)
        done = [rn for rn, (total, confirmed) in completion.items() if total and confirmed == total]
        return max(done) if done else None


def _club_squad_positions(cursor: sqlite3.Cursor, canon: str) -> list[tuple[str, str]]:
    """[(player_name, stored position)] of one club, in upload order (the XI first)."""
    t_norm = normalize_team_name(canon)
    cursor.execute(
        "SELECT player_name, position FROM squad_players "
        "WHERE norm_team_name = ? OR LOWER(team_name) = LOWER(?) ORDER BY id ASC",
        (t_norm, canon)
    )
    return [((r["player_name"] or "").strip(), (r["position"] or "").strip()) for r in cursor.fetchall()]


def _offline_squad_positions(squad: list[tuple[str, str]]) -> dict[str, str]:
    """{player_key: position} for a squad, resolved without any network call.

    The stored position is overridden by the built-in registry. A lineup
    screenshot lists the goalkeeper last of the XI, and a goalkeeper the
    detector never recognised is stored under its default 'ST' — so when a
    club has no goalkeeper among its starters and the 11th starter sits on
    that default, he is taken as the goalkeeper.
    """
    from services.player_positions import known_position, normalize_position

    positions: dict[str, str] = {}
    for idx, (name, stored) in enumerate(squad):
        key = normalize_player_name_key(name) or name.lower()
        if key in positions:
            continue
        pos = known_position(name) or (normalize_position(stored) if stored else "ST")
        positions[key] = pos

    starters = squad[:TOTW_STARTERS]
    starter_keys = [normalize_player_name_key(n) or n.lower() for n, _ in starters]
    if len(starters) == TOTW_STARTERS and not any(positions.get(k) == "GK" for k in starter_keys):
        last_name, last_stored = starters[-1]
        last_key = starter_keys[-1]
        if not known_position(last_name) and normalize_position(last_stored or "ST") == "ST":
            positions[last_key] = "GK"
    return positions


def get_totw_stats(
    start_round: int,
    end_round: int,
    division_id: int,
    season_id: int | None = None,
) -> list[dict]:
    """Per-player numbers of a block of rounds — the candidate pool of the TOTW.

    Every row: player_name, team_name, position, is_starter, goals, assists,
    mvp, braces (matches with 2+ goals), and the club's block figures
    matches, wins, clean_sheets, goals_conceded.

    The pool is everyone with a goal, an assist or an MVP crown in the block,
    plus the XI of every club that played in it (the first 11 squad rows),
    so a defender or a goalkeeper with no goal still has his clean sheets
    counted. Clean sheets go to starters only — the reserves of a squad did
    not necessarily play. Technical results carry no events and no football
    was played, so they count for nothing here beyond the confirmation that
    closed the block.

    Positions are resolved offline (stored squad position + built-in
    registry); this runs inside a background job and must not go online.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        cursor.execute("""
            SELECT m.id, m.player1_team, m.player2_team, m.player1_score, m.player2_score,
                   m.mvp_player, COALESCE(m.is_technical, 0) AS is_technical
            FROM matches m
            WHERE m.division_id = ?
              AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND (m.season_id = ? OR m.season_id IS NULL)
              AND m.round_number BETWEEN ? AND ?
              AND m.status = 'confirmed'
            ORDER BY m.round_number, m.id
        """, (division_id, target_season_id, start_round, end_round))
        matches = [dict(r) for r in cursor.fetchall() if not r["is_technical"]]
        if not matches:
            return []

        def canon_of(team: str | None) -> str:
            team = (team or "").strip()
            return (resolve_team_name(team) or team) if team else ""

        # Club block figures.
        clubs: dict[str, dict] = {}
        for m in matches:
            s1, s2 = int(m["player1_score"] or 0), int(m["player2_score"] or 0)
            for team, scored, conceded in ((m["player1_team"], s1, s2), (m["player2_team"], s2, s1)):
                canon = canon_of(team)
                if not canon:
                    continue
                c = clubs.setdefault(normalize_team_name(canon), {
                    "team_name": canon, "matches": 0, "wins": 0, "clean_sheets": 0, "goals_conceded": 0,
                })
                c["matches"] += 1
                c["wins"] += 1 if scored > conceded else 0
                c["clean_sheets"] += 1 if conceded == 0 else 0
                c["goals_conceded"] += conceded

        fold = _player_folder(cursor)
        players: dict[tuple[str, str], dict] = {}

        def entry(team: str | None, name: str | None) -> dict | None:
            if not (name or "").strip():
                return None
            club_key, player_key, display = fold(team, name)
            if not club_key:
                return None
            row = players.get((club_key, player_key))
            if row is None:
                row = players[(club_key, player_key)] = {
                    "player_name": display,
                    "team_name": clubs.get(club_key, {}).get("team_name") or canon_of(team),
                    "_club_key": club_key,
                    "_player_key": player_key,
                    "goals": 0, "assists": 0, "mvp": 0, "braces": 0, "is_starter": False,
                }
            return row

        # Goals / assists, per match so braces can be told apart.
        cursor.execute("""
            SELECT me.match_id, me.team_name, me.player_name, me.event_type, SUM(me.count) AS cnt
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE m.division_id = ?
              AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND (m.season_id = ? OR m.season_id IS NULL)
              AND m.round_number BETWEEN ? AND ?
              AND m.status = 'confirmed'
              AND COALESCE(m.is_technical, 0) = 0
            GROUP BY me.match_id, me.team_name, me.player_name, me.event_type
            ORDER BY MIN(me.id)
        """, (division_id, target_season_id, start_round, end_round))
        per_match_goals: dict[tuple[int, tuple[str, str]], int] = {}
        for r in cursor.fetchall():
            row = entry(r["team_name"], r["player_name"])
            if row is None:
                continue
            cnt = int(r["cnt"] or 0)
            if r["event_type"] == "goal":
                row["goals"] += cnt
                k = (r["match_id"], (row["_club_key"], row["_player_key"]))
                per_match_goals[k] = per_match_goals.get(k, 0) + cnt
            elif r["event_type"] == "assist":
                row["assists"] += cnt
        for (_mid, pkey), goals in per_match_goals.items():
            if goals >= 2:
                players[pkey]["braces"] += 1

        # MVP crowns: the club is the side whose events or squad name him.
        for m in matches:
            name = (m["mvp_player"] or "").strip()
            if not name:
                continue
            cursor.execute(
                "SELECT team_name FROM match_events WHERE match_id = ? "
                "AND LOWER(TRIM(player_name)) = LOWER(?) LIMIT 1",
                (m["id"], name)
            )
            hit = cursor.fetchone()
            team = hit["team_name"] if hit else ""
            if not team:
                hits = []
                for side in (m["player1_team"], m["player2_team"]):
                    found = match_roster_name(name, _load_club_roster(cursor, side))
                    if found:
                        hits.append((side, found))
                if len(hits) == 1:
                    team, name = hits[0]
            if not team:
                continue
            row = entry(team, name)
            if row is not None:
                row["mvp"] += 1

        # Starting XI of every club that played, and offline positions.
        positions_by_club: dict[str, dict[str, str]] = {}
        for club_key, club in clubs.items():
            squad = _club_squad_positions(cursor, club["team_name"])
            positions_by_club[club_key] = _offline_squad_positions(squad)
            for name, _pos in squad[:TOTW_STARTERS]:
                row = entry(club["team_name"], name)
                if row is not None:
                    row["is_starter"] = True

        from services.player_positions import known_position

        result: list[dict] = []
        for row in players.values():
            club = clubs.get(row["_club_key"])
            if club is None:
                continue
            pos = positions_by_club.get(row["_club_key"], {}).get(row["_player_key"])
            if not pos:
                pos = known_position(row["player_name"]) or (
                    "CAM" if row["assists"] > row["goals"] else "ST"
                )
            out = {k: v for k, v in row.items() if not k.startswith("_")}
            out.update({
                "team_name": club["team_name"],
                "position": pos,
                "matches": club["matches"],
                "wins": club["wins"],
                "clean_sheets": club["clean_sheets"] if row["is_starter"] else 0,
                "goals_conceded": club["goals_conceded"],
            })
            result.append(out)
        result.sort(key=lambda r: (-(r["goals"] + r["assists"] + r["mvp"]), r["team_name"], r["player_name"]))
        return result


def get_recent_confirmed_matches(
    limit: int = 15,
    division_id: int | None = None,
    season_id: int | None = None,
) -> list[dict]:
    """Retrieve recent confirmed matches, optionally scoped to one division and season.

    With division_id=None the legacy cross-division behaviour is kept for callers
    that intentionally want a league-wide feed.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        query = """
            SELECT
                m.id, m.round_number, m.player1_score, m.player2_score,
                m.division_id, m.season_id,
                COALESCE(m.player1_team, u1.team_name) AS team1, u1.username AS user1,
                COALESCE(m.player2_team, u2.team_name) AS team2, u2.username AS user2
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.status = 'confirmed'
        """
        params: list = []

        if division_id is not None:
            target_season_id = season_id
            if target_season_id is None:
                act = get_active_season()
                target_season_id = act["id"] if act else 1
            query += " AND m.division_id = ? AND (m.season_id = ? OR m.season_id IS NULL)"
            params.extend([division_id, target_season_id])

        query += " ORDER BY m.id DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        return [dict(row) for row in cursor.fetchall()]

def get_all_squads() -> dict[str, list[str]]:
    """Retrieve all player squads grouped by team_name."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name, player_name FROM squad_players ORDER BY team_name, id ASC")
        squads = {}
        for row in cursor.fetchall():
            team = row["team_name"]
            if team not in squads:
                squads[team] = []
            squads[team].append(row["player_name"])
        return squads


def get_all_unique_players() -> list[tuple[str, str]]:
    """Retrieve all unique (player_name, team_name) tuples from squad_players and match_events."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT player_name, team_name FROM squad_players
            UNION
            SELECT player_name, team_name FROM match_events WHERE player_name IS NOT NULL AND player_name != ''
            ORDER BY team_name, player_name ASC
        """)
        return [(row["player_name"], row["team_name"]) for row in cursor.fetchall()]


def detect_teams_from_players(
    side1_players: list[str],
    side2_players: list[str],
    caption: str | None = None
) -> tuple[str | None, str | None]:
    """
    Determines team1 (left side) and team2 (right side) from OCR-extracted player names
    (goals and assists) by matching them against club squads in the database.
    Also incorporates any club names mentioned in user caption.
    Returns (team1_name, team2_name).
    """
    all_squads = get_all_squads()
    all_teams = get_all_teams()
    for t in all_teams:
        if t not in all_squads:
            all_squads[t] = []

    caption_clean = (caption or "").lower()
    caption_teams = []
    for t in all_teams:
        t_clean = t.lower()
        t_norm = normalize_team_name(t)
        if t_clean in caption_clean or (t_norm and t_norm in normalize_team_name(caption_clean)):
            caption_teams.append(t)

    # Check words in caption with resolve_team_name
    if caption:
        for word in re.split(r'[\s,:;\-_/\\|]+', caption):
            resolved = resolve_team_name(word)
            if resolved and resolved in all_teams and resolved not in caption_teams:
                caption_teams.append(resolved)

    def score_side(player_list, squad_list):
        score = 0
        for p in player_list:
            if not p:
                continue
            p_clean = str(p).strip().lower()
            if not p_clean:
                continue
            for sp in squad_list:
                sp_clean = str(sp).strip().lower()
                if p_clean == sp_clean:
                    score += 6
                    break
                elif len(p_clean) >= 4 and (p_clean in sp_clean or sp_clean in p_clean):
                    score += 4
                    break
                else:
                    p_parts = [x for x in p_clean.split() if len(x) >= 3]
                    sp_parts = [x for x in sp_clean.split() if len(x) >= 3]
                    if p_parts and sp_parts and any(x in sp_parts for x in p_parts):
                        score += 3
                        break
        return score

    # Compute scores for every team on side1 and side2
    side1_scores = {}
    side2_scores = {}
    for team, squad in all_squads.items():
        s1 = score_side(side1_players, squad)
        s2 = score_side(side2_players, squad)
        # If team is explicitly in caption, give a bonus
        if team in caption_teams:
            s1 += 2
            s2 += 2
        side1_scores[team] = s1
        side2_scores[team] = s2

    s1_sorted = sorted(side1_scores.items(), key=lambda x: x[1], reverse=True)
    s2_sorted = sorted(side2_scores.items(), key=lambda x: x[1], reverse=True)

    best_t1, best_s1 = s1_sorted[0] if s1_sorted else (None, 0)

    best_t2 = None
    best_s2 = 0
    for t, s in s2_sorted:
        if t != best_t1:
            best_t2 = t
            best_s2 = s
            break

    # If side1 or side2 had no player matches, fill from caption if available
    if (best_s1 == 0 or not best_t1) and caption_teams:
        for ct in caption_teams:
            if ct != best_t2:
                best_t1 = ct
                break
    if (best_s2 == 0 or not best_t2) and caption_teams:
        for ct in caption_teams:
            if ct != best_t1:
                best_t2 = ct
                break

    return best_t1, best_t2




def get_player_card_stats(player_name: str, team_name: str) -> dict:
    """
    Get full season stats for a specific player:
    - total goals and assists (overall, league, cup)
    - per-round / per-stage breakdown.
    Only considers confirmed matches.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        p_name = player_name.strip()
        t_name = team_name.strip()

        # Every spelling of this footballer in the club's events (EMEGA, Emegha):
        # the card sums them all, the same way the club aggregators fold them.
        fold = _player_folder(cursor)
        target_key = fold(t_name, p_name)[1]
        cursor.execute(
            "SELECT DISTINCT player_name FROM match_events "
            "WHERE LOWER(team_name) = LOWER(?) AND player_name IS NOT NULL AND player_name != ''",
            (t_name,)
        )
        spellings = {r["player_name"].strip().lower() for r in cursor.fetchall()
                     if fold(t_name, r["player_name"])[1] == target_key}
        spellings.add(p_name.lower())
        spellings = sorted(spellings)
        name_in = ",".join("?" * len(spellings))

        # 1. Total overall goals & assists + breakdown by tournament
        cursor.execute("""
            SELECT 
                COALESCE(SUM(CASE WHEN me.event_type = 'goal' THEN me.count ELSE 0 END), 0) AS total_goals,
                COALESCE(SUM(CASE WHEN me.event_type = 'assist' THEN me.count ELSE 0 END), 0) AS total_assists,
                COALESCE(SUM(CASE WHEN me.event_type = 'goal' AND (m.tournament_type IS NULL OR m.tournament_type = 'league') AND m.round_number > 0 THEN me.count ELSE 0 END), 0) AS league_goals,
                COALESCE(SUM(CASE WHEN me.event_type = 'assist' AND (m.tournament_type IS NULL OR m.tournament_type = 'league') AND m.round_number > 0 THEN me.count ELSE 0 END), 0) AS league_assists,
                COALESCE(SUM(CASE WHEN me.event_type = 'goal' AND (m.tournament_type = 'cup' OR m.round_number = -1 OR (m.cup_series_id IS NOT NULL AND m.cup_series_id > 0)) THEN me.count ELSE 0 END), 0) AS cup_goals,
                COALESCE(SUM(CASE WHEN me.event_type = 'assist' AND (m.tournament_type = 'cup' OR m.round_number = -1 OR (m.cup_series_id IS NOT NULL AND m.cup_series_id > 0)) THEN me.count ELSE 0 END), 0) AS cup_assists
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE LOWER(TRIM(me.player_name)) IN ({name_in})
              AND LOWER(me.team_name) = LOWER(?)
              AND m.status = 'confirmed'
        """.format(name_in=name_in), (*spellings, t_name))
        summary_row = cursor.fetchone()
        summary_dict = dict(summary_row) if summary_row else {}
        
        total_goals = summary_dict.get("total_goals", 0)
        total_assists = summary_dict.get("total_assists", 0)
        league_goals = summary_dict.get("league_goals", 0)
        league_assists = summary_dict.get("league_assists", 0)
        cup_goals = summary_dict.get("cup_goals", 0)
        cup_assists = summary_dict.get("cup_assists", 0)

        # 2. Detailed per-tour / stage breakdown
        cursor.execute("""
            SELECT
                m.round_number,
                m.tournament_type,
                m.cup_stage,
                m.cup_series_id,
                CASE 
                    WHEN LOWER(m.player1_team) = LOWER(?) THEN m.player2_team 
                    ELSE m.player1_team 
                END AS opponent,
                me.event_type,
                SUM(me.count) AS total
            FROM match_events me
            JOIN matches m ON me.match_id = m.id
            WHERE LOWER(TRIM(me.player_name)) IN ({name_in})
              AND LOWER(me.team_name) = LOWER(?)
              AND m.status = 'confirmed'
            GROUP BY 
                CASE 
                    WHEN m.tournament_type = 'cup' OR m.round_number = -1 OR (m.cup_series_id IS NOT NULL AND m.cup_series_id > 0) THEN -1
                    ELSE m.round_number
                END,
                me.event_type
            ORDER BY 
                CASE 
                    WHEN m.tournament_type = 'cup' OR m.round_number = -1 OR (m.cup_series_id IS NOT NULL AND m.cup_series_id > 0) THEN -1
                    ELSE m.round_number
                END ASC
        """.format(name_in=name_in), (t_name, *spellings, t_name))

        rows = cursor.fetchall()
        
        # Build grouped rounds dict: {round_key: {"title": str, "opponent": str, "goals": int, "assists": int, "is_cup": bool}}
        rounds_dict = {}
        for r in rows:
            rn = r["round_number"]
            is_cup = bool(r["tournament_type"] == "cup" or rn == -1 or (r["cup_series_id"] and r["cup_series_id"] > 0))
            opp = "" if is_cup else (r["opponent"] or "")
            
            key = -1 if is_cup else rn
            if key not in rounds_dict:
                title = "Кубок" if is_cup else f"Тур {rn}"
                rounds_dict[key] = {
                    "round_key": key,
                    "title": title,
                    "opponent": opp,
                    "goals": 0,
                    "assists": 0,
                    "is_cup": is_cup
                }
            elif opp and not rounds_dict[key].get("opponent"):
                rounds_dict[key]["opponent"] = opp

            if r["event_type"] == "goal":
                rounds_dict[key]["goals"] += r["total"]
            elif r["event_type"] == "assist":
                rounds_dict[key]["assists"] += r["total"]

        items = []
        for k in sorted(rounds_dict.keys()):
            item = rounds_dict[k]
            item["total"] = item["goals"] + item["assists"]
            items.append(item)

        return {
            "player_name": player_name,
            "team_name": team_name,
            "position": get_player_position(player_name, team_name),
            "total_goals": total_goals,
            "total_assists": total_assists,
            "total_points": total_goals + total_assists,
            "league_goals": league_goals,
            "league_assists": league_assists,
            "cup_goals": cup_goals,
            "cup_assists": cup_assists,
            "items": items,
            "rounds": {item["round_key"]: {"goals": item["goals"], "assists": item["assists"]} for item in items},
        }


def get_all_cup_series() -> list[dict]:
    """Return every cup series (newest stage first) for context/summary rendering."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, stage, series_num, team1_name, team2_name,
                   team1_wins, team2_wins, winner_name, status
            FROM cup_series
            ORDER BY id ASC
        """)
        return [dict(r) for r in cursor.fetchall()]


# ═════════════════════════════════════════════════════════════════════════════
# 🏆 ОБЩИЙ КУБОК — СТАДИИ, СЕТКА, МАТЧИ СЕРИИ
# ═════════════════════════════════════════════════════════════════════════════


CUP_STAGE_KEY_SEP = "@D"


def cup_scope(division_id) -> int | None:
    """Кубок по номеру дивизиона: None — общий. 0 (sentinel кубковых матчей) и
    пустое значение тоже означают общий кубок."""
    try:
        value = int(division_id) if division_id not in (None, "") else 0
    except (TypeError, ValueError):
        raise ValueError(f"Неверный дивизион кубка: {division_id!r}")
    return value or None


def cup_stage_key(stage: str, division_id: int | None = None) -> str:
    """Ключ этапа в `cup_stages.stage`: у общего кубка это имя стадии, у кубка
    дивизиона — «1/8@D3» (см. миграцию 027: UNIQUE(season_id, stage) не снять)."""
    scope = cup_scope(division_id)
    return stage if scope is None else f"{stage}{CUP_STAGE_KEY_SEP}{scope}"


def split_cup_stage_key(key: str) -> tuple[str, int | None]:
    """Обратная `cup_stage_key`: (имя стадии, дивизион или None)."""
    key = str(key or "")
    if CUP_STAGE_KEY_SEP in key:
        stage, _, div = key.rpartition(CUP_STAGE_KEY_SEP)
        if div.isdigit():
            return stage, int(div)
    return key, None


def _cup_stage_dict(row) -> dict | None:
    """Строка `cup_stages` для читателей: `stage` — обычное имя стадии,
    `stage_key` — то, что лежит в базе, `division_id` — кубок (None — общий)."""
    if row is None:
        return None
    d = dict(row)
    stage, div = split_cup_stage_key(d.get("stage"))
    d["stage_key"] = d.get("stage")
    d["stage"] = stage
    d["division_id"] = cup_scope(d.get("division_id") or div)
    return d


def cup_scope_short(division_id) -> str:
    """Короткая метка кубка: «Общий» или «Д3» (по коду DIV_N, иначе имя)."""
    scope = cup_scope(division_id)
    if scope is None:
        return "Общий"
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, code FROM divisions WHERE id = ?", (scope,))
        row = cursor.fetchone()
    code = ((row["code"] if row else "") or "").strip().upper()
    if code.startswith("DIV_") and code[4:].isdigit():
        return f"Д{int(code[4:])}"
    return (row["name"] if row and row["name"] else f"Д{scope}")


def cup_scope_label(division_id) -> str:
    """Название кубка для сообщений: «Общий кубок» / «Кубок Д3»."""
    scope = cup_scope(division_id)
    return "Общий кубок" if scope is None else f"Кубок {cup_scope_short(scope)}"


def cup_stage_title(stage_key: str) -> str:
    """Этап для текста сообщений: «1/8» у общего кубка, «1/8 · Кубок Д3» у дивизиона."""
    stage, div = split_cup_stage_key(stage_key)
    return stage if div is None else f"{stage} · {cup_scope_label(div)}"


def create_cup_stage(
    stage: str,
    season_id: int | None = None,
    deadline: str | None = None,
    division_id: int | None = None,
) -> int:
    """Завести этап кубка (общего или дивизиона). Повторный вызов возвращает уже
    существующую строку.

    Идемпотентность здесь нужна не для красоты: `create_cup_series` обеспечивает
    стадию сам, и без этого он плодил бы дубли этапов на каждом запуске жеребьёвки.
    """
    if stage not in CUP_STAGE_ORDER:
        raise ValueError(
            f"Неизвестная стадия кубка: {stage!r}. Допустимы: {', '.join(CUP_STAGES)}."
        )
    scope = cup_scope(division_id)
    key = cup_stage_key(stage, scope)
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        if scope is not None:
            cursor.execute("SELECT 1 FROM divisions WHERE id = ?", (scope,))
            if not cursor.fetchone():
                raise ValueError(f"Дивизион #{scope} не найден — кубок дивизиона не завести.")
        cursor.execute(
            "SELECT id FROM cup_stages WHERE season_id = ? AND stage = ? LIMIT 1",
            (s_id, key)
        )
        row = cursor.fetchone()
        if row:
            return row["id"]
        cursor.execute(
            """
            INSERT INTO cup_stages (season_id, stage, stage_order, deadline, division_id, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            """,
            (s_id, key, CUP_STAGE_ORDER[stage], deadline, scope)
        )
        return cursor.lastrowid


def get_cup_stage(stage: str, season_id: int | None = None, division_id: int | None = None) -> dict | None:
    """Строка этапа по имени стадии в кубке `division_id` (None — общий)."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cup_stages WHERE season_id = ? AND stage = ? LIMIT 1",
            (s_id, cup_stage_key(stage, division_id))
        )
        return _cup_stage_dict(cursor.fetchone())


def get_cup_stage_by_id(stage_id: int) -> dict | None:
    """Строка этапа по id — тем же ключом работают кнопки панели админа."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM cup_stages WHERE id = ? LIMIT 1", (stage_id,))
        return _cup_stage_dict(cursor.fetchone())


def list_cup_stages(season_id: int | None = None, division_id: int | None = None) -> list[dict]:
    """Этапы одного кубка сезона в порядке игры: общего (по умолчанию) или дивизиона."""
    s_id = _resolve_season_id(season_id)
    scope = cup_scope(division_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM cup_stages WHERE season_id = ? ORDER BY stage_order ASC, id ASC",
            (s_id,)
        )
        rows = [_cup_stage_dict(r) for r in cursor.fetchall()]
    return [r for r in rows if r["division_id"] == scope]


def list_cup_scopes(season_id: int | None = None) -> list[int | None]:
    """Кубки сезона, у которых есть хотя бы один этап: None (общий) первым,
    затем дивизионы по возрастанию."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT stage, division_id FROM cup_stages WHERE season_id = ?", (s_id,))
        scopes = {_cup_stage_dict(r)["division_id"] for r in cursor.fetchall()}
    return sorted(scopes, key=lambda d: -1 if d is None else d)


def count_cup_stage_matches(stage_id: int) -> int:
    """Сколько матчей этапа уже заведено — по нему решается, есть ли что выставлять."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM matches WHERE stage_id = ?", (stage_id,))
        return int(cursor.fetchone()[0])


def open_cup_stage_bets(stage_id: int, actor_id: int | None = None) -> tuple[bool, str]:
    """Открыть приём прогнозов на этап.

    Тот же инвариант, что у лиги: `is_open = 1 AND bets_open = 1` недопустимо,
    поэтому линию нельзя вернуть этапу, который уже играют. Матчей на этапе нет —
    открывать нечего (селектор линии иначе показывает пустой этап как живой).
    """
    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, stage, is_open, COALESCE(bets_open, 0) AS bets_open FROM cup_stages WHERE id = ?", (stage_id,))
        stage_row = cursor.fetchone()
        if not stage_row:
            return False, f"Этап кубка #{stage_id} не найден."
        title = cup_stage_title(stage_row["stage"])
        if stage_row["is_open"]:
            return False, f"Этап {title} уже открыт для игры — приём прогнозов закрыт."
        if stage_row["bets_open"]:
            return False, f"Приём прогнозов на этап {title} уже открыт."

        # `transaction()` ре-ентрельна, так что счётчик остаётся внутри этого же
        # заблокированного транзакционного скоупа — второго чтения тех же строк нет.
        if count_cup_stage_matches(stage_id) == 0:
            return False, f"На этапе {title} нет матчей — выставлять в линию нечего."

        cursor.execute(
            "UPDATE cup_stages SET bets_open = 1, bets_opened_at = ? WHERE id = ?",
            (now_msk_str(), stage_id)
        )
    logger.info("Cup stage #%s (%s) betting line opened by %s", stage_id, title, actor_id)
    return True, f"Приём прогнозов на этап {title} открыт."


def start_cup_stage(stage_id: int, actor_id: int | None = None) -> tuple[bool, str]:
    """Открыть этап для игры; линия закрывается тем же переходом."""
    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, stage, is_open FROM cup_stages WHERE id = ?", (stage_id,))
        stage_row = cursor.fetchone()
        if not stage_row:
            return False, f"Этап кубка #{stage_id} не найден."
        title = cup_stage_title(stage_row["stage"])
        if stage_row["is_open"]:
            return False, f"Этап {title} уже открыт."
        cursor.execute(
            "UPDATE cup_stages SET is_open = 1, bets_open = 0, opened_at = ?, opened_by = ? WHERE id = ?",
            (now_msk_str(), actor_id, stage_id)
        )
        # Та же линия, что у лиги: закрытый приём прогнозов должен быть виден и в
        # самих рынках, иначе Mini App продолжает показывать кубковую пару как
        # доступную для ставки, а отказ приходит только на попытке поставить.
        _close_line_scope(cursor, "SELECT id FROM matches WHERE stage_id = ?", (stage_id,))
    logger.info("Cup stage #%s (%s) opened for play by %s", stage_id, title, actor_id)
    return True, f"Этап {title} открыт для игры."


def cup_results_closed_reason(match_id: int) -> str | None:
    """Почему результат кубковой игры сейчас вносить нельзя — или None, если можно.

    Этап живёт в две фазы, как тур лиги: пока открыта линия (`is_open = 0`),
    игры закрыты для результатов, иначе ставку можно было бы сделать на уже
    сыгранный матч. Ввод открывает только «▶ начать этап», тот же переход,
    что закрывает линию.

    Лиговый матч, заголовок серии и игра без `stage_id` сюда не относятся:
    на игру без этапа не принимается ни одной ставки (гейт отвечает
    STAGE_NOT_FOUND), так что и защищать в ней нечего.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT m.tournament_type, COALESCE(m.is_series_header, 0) AS is_series_header, "
            "cs.stage, cs.is_open "
            "FROM matches m JOIN cup_stages cs ON cs.id = m.stage_id "
            "WHERE m.id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
    if not row or not match_is_cup(row) or row["is_series_header"] or row["is_open"]:
        return None
    return (
        f"Этап {cup_stage_title(row['stage'])} ещё не начат: пока идёт приём прогнозов, результаты "
        "не принимаются. Ввод откроется после старта этапа."
    )


def create_cup_series(
    stage: str,
    pairs: list[tuple[str, str]],
    season_id: int | None = None,
    division_id: int | None = None,
) -> list[int]:
    """Завести сетку этапа по готовым парам; порядок пар = номера серий.

    `division_id` — кубок дивизиона: тогда каждый клуб обязан быть в составе
    этого дивизиона (`get_division_teams`), иначе в кубок Д3 попал бы клуб Д1.

    Валидация целиком до первой записи: жеребьёвка, у которой один клуб встречается
    в двух парах, даёт сетку, где клуб выбывает и не выбывает одновременно, а
    починить её потом можно только переигрыванием матчей.

    Стадия обеспечивается здесь, а не ожидается готовой: заводить этап отдельной
    кнопкой — значит иметь путь, где серии существуют без строки `cup_stages`, и
    гейт на них отвечает отказом.
    """
    s_id = _resolve_season_id(season_id)
    scope = cup_scope(division_id)
    if not pairs:
        raise ValueError("Список пар пуст — сетку этапа не из чего построить.")
    roster: dict[str, str] | None = None
    if scope is not None:
        roster = {t.lower(): t for t in get_division_teams(scope, season_id=s_id)}
        if not roster:
            raise ValueError(f"У дивизиона #{scope} нет клубов — кубок дивизиона не из кого собрать.")

    canonical: list[tuple[str, str]] = []
    seen: dict[str, str] = {}
    for index, pair in enumerate(pairs, start=1):
        try:
            raw1, raw2 = pair
        except (TypeError, ValueError):
            raise ValueError(f"Пара #{index} должна быть из двух клубов: {pair!r}")
        t1 = (resolve_team_name(raw1) or str(raw1).strip())
        t2 = (resolve_team_name(raw2) or str(raw2).strip())
        if not t1 or not t2:
            raise ValueError(f"Пара #{index}: имя клуба пустое.")
        if t1.lower() == t2.lower():
            raise ValueError(f"Пара #{index}: клуб играет сам с собой ({t1}).")
        for club in (t1, t2):
            key = club.lower()
            if roster is not None and key not in roster:
                raise ValueError(
                    f"Пара #{index}: {club} не из дивизиона {cup_scope_short(scope)} — в его кубок клуб не завести."
                )
            if key in seen:
                raise ValueError(f"{club} встречается в сетке дважды: в парах {seen[key]} и {index}.")
            seen[key] = str(index)
        canonical.append((t1, t2))

    stage_id = create_cup_stage(stage, season_id=s_id, division_id=scope)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM cup_series WHERE stage_id = ?", (stage_id,))
        existing = int(cursor.fetchone()[0])
        if existing:
            raise ValueError(
                f"Стадия {cup_stage_title(cup_stage_key(stage, scope))}: сетка уже заведена ({existing} сер.). "
                "Правь серии поштучно, а не повторной жеребьёвкой."
            )
        ids: list[int] = []
        for num, (t1, t2) in enumerate(canonical, start=1):
            cursor.execute(
                """
                INSERT INTO cup_series (stage, series_num, team1_name, team2_name,
                                        team1_wins, team2_wins, status, stage_id)
                VALUES (?, ?, ?, ?, 0, 0, 'active', ?)
                """,
                (stage, num, t1, t2, stage_id)
            )
            ids.append(cursor.lastrowid)
    return ids


def get_cup_bracket(stage: str, season_id: int | None = None, division_id: int | None = None) -> list[dict]:
    """Серии этапа кубка `division_id` (None — общий) в порядке номеров."""
    stage_row = get_cup_stage(stage, season_id=season_id, division_id=division_id)
    if not stage_row:
        return []
    return get_cup_stage_series(stage_row["id"])


def get_cup_stage_series(stage_id: int) -> list[dict]:
    """Серии этапа по его id в порядке номеров."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT cs.id, cs.stage, cs.series_num, cs.team1_name, cs.team2_name,
                   cs.team1_wins, cs.team2_wins, cs.winner_name, cs.status, cs.stage_id
            FROM cup_series cs
            WHERE cs.stage_id = ?
            ORDER BY cs.series_num ASC
            """,
            (stage_id,)
        )
        return [dict(r) for r in cursor.fetchall()]


def get_cup_full_bracket(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Весь кубок по этапам: [{"stage": строка этапа, "series": [...]}, ...] в
    порядке игры — источник Pillow-сетки и сетки Mini App."""
    return [
        {"stage": st, "series": get_cup_stage_series(st["id"])}
        for st in list_cup_stages(season_id=season_id, division_id=division_id)
    ]


def get_match_cup_scope(match_id: int) -> dict | None:
    """Кубок, к которому относится матч, — или None для лиги.

    Кубковые матчи лежат на sentinel-дивизионе 0, так что сам матч не говорит,
    чей это кубок; ответ даёт его этап (`cup_stages.division_id`).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT m.tournament_type, m.cup_series_id, st.* "
            "FROM matches m LEFT JOIN cup_stages st ON st.id = m.stage_id "
            "WHERE m.id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
    if not row or not match_is_cup(row) or row["id"] is None:
        return None
    stage = _cup_stage_dict({k: row[k] for k in row.keys() if k not in ("tournament_type", "cup_series_id")})
    return {
        "division_id": stage["division_id"],
        "season_id": stage["season_id"],
        "stage_id": stage["id"],
        "stage": stage["stage"],
        "series_id": row["cup_series_id"],
    }


def cup_label_of(stage_key=None, division_id=None, code=None, name=None) -> str:
    """«Кубок Д3» / «Общий кубок» по уже выбранным полям этапа — без запроса.

    `division_id` — `cup_stages.division_id`; если он пуст, дивизион берётся из
    ключа этапа («1/8@D3»). Кубковые матчи лежат на sentinel-дивизионе 0, так
    что `matches.division_id` для подписи не годится.
    """
    div = division_id or split_cup_stage_key(stage_key or "")[1]
    if not div:
        return "Общий кубок"
    code = (code or "").strip().upper()
    if code.startswith("DIV_") and code[4:].isdigit():
        return f"Кубок Д{int(code[4:])}"
    return f"Кубок {name or f'Д{div}'}"


def get_match_cup_labels(match_ids) -> dict[int, str]:
    """Подписи кубков для пачки матчей: {match_id: «Кубок Д3» | «Общий кубок»}.

    В ответ попадают только кубковые матчи — у лиги подпись даёт дивизион.
    """
    ids = sorted({int(i) for i in match_ids or [] if i is not None})
    if not ids:
        return {}
    labels: dict[int, str] = {}
    with transaction() as conn:
        cursor = conn.cursor()
        for start in range(0, len(ids), 500):
            chunk = ids[start:start + 500]
            cursor.execute(
                "SELECT m.id, m.tournament_type, m.cup_series_id, st.stage AS stage_key, "
                "st.division_id, cd.code, cd.name "
                "FROM matches m "
                "LEFT JOIN cup_series cs ON cs.id = m.cup_series_id "
                "LEFT JOIN cup_stages st ON st.id = COALESCE(m.stage_id, cs.stage_id) "
                "LEFT JOIN divisions cd ON cd.id = st.division_id "
                f"WHERE m.id IN ({','.join('?' * len(chunk))})",
                chunk,
            )
            for row in cursor.fetchall():
                if match_is_cup(row):
                    labels[row["id"]] = cup_label_of(row["stage_key"], row["division_id"],
                                                     row["code"], row["name"])
    return labels


def _attach_cup_labels(rows: list[dict], key: str = "match_id") -> list[dict]:
    """Проставляет `cup_label` кубковым строкам (Mini App пишет его вместо дивизиона)."""
    labels = get_match_cup_labels(r.get(key) for r in rows)
    for r in rows:
        r["cup_label"] = labels.get(r.get(key))
    return rows


# ─── Темы кубков: «Кубок» в форуме группы, закреплённая сетка ─────────────────
# Живут в `cup_topics` (миграция 023): строка на (сезон, кубок). `topic_type` —
# «cup» у общего кубка и «cup_div_<id>» у кубка дивизиона, `message_thread_id` —
# тема форума, `anchor_message_id` — закреплённое сообщение с Pillow-сеткой,
# которое бот редактирует по мере внесения результатов.

def _cup_topic_type(division_id) -> str:
    scope = cup_scope(division_id)
    return "cup" if scope is None else f"cup_div_{scope}"


def set_cup_topic(
    division_id: int | None,
    group_chat_id: int,
    message_thread_id: int,
    season_id: int | None = None,
) -> None:
    """Привязать тему форума к кубку. Закреп прошлой темы забывается: сетку в
    новой теме бот публикует заново. Тема, которая была темой дивизиона, этой
    привязкой у дивизиона отбирается — одна тема служит одному назначению."""
    s_id = _resolve_season_id(season_id)
    topic_type = _cup_topic_type(division_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM division_topics WHERE group_chat_id = ? AND message_thread_id = ?",
            (group_chat_id, message_thread_id)
        )
        cursor.execute(
            "DELETE FROM cup_topics WHERE season_id = ? AND group_chat_id = ? "
            "AND message_thread_id = ? AND topic_type != ?",
            (s_id, group_chat_id, message_thread_id, topic_type)
        )
        cursor.execute(
            """
            INSERT INTO cup_topics (season_id, topic_type, group_chat_id, message_thread_id,
                                    anchor_message_id, created_at)
            VALUES (?, ?, ?, ?, NULL, datetime('now', '+3 hours'))
            ON CONFLICT(season_id, topic_type) DO UPDATE SET
                group_chat_id = excluded.group_chat_id,
                message_thread_id = excluded.message_thread_id,
                anchor_message_id = NULL
            """,
            (s_id, topic_type, group_chat_id, message_thread_id)
        )


def get_cup_topic(division_id: int | None, season_id: int | None = None) -> dict | None:
    """Тема кубка: {group_chat_id, message_thread_id, anchor_message_id} или None."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT group_chat_id, message_thread_id, anchor_message_id FROM cup_topics "
            "WHERE season_id = ? AND topic_type = ? AND message_thread_id != 0 LIMIT 1",
            (s_id, _cup_topic_type(division_id))
        )
        row = cursor.fetchone()
    return dict(row) if row else None


def clear_cup_topic(division_id: int | None, season_id: int | None = None) -> bool:
    """Снять привязку темы кубка. True — если было что снимать."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM cup_topics WHERE season_id = ? AND topic_type = ?",
            (s_id, _cup_topic_type(division_id))
        )
        return cursor.rowcount > 0


def set_cup_bracket_anchor(division_id: int | None, message_id: int | None, season_id: int | None = None) -> None:
    """Запомнить закреплённое сообщение с сеткой кубка."""
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cup_topics SET anchor_message_id = ? WHERE season_id = ? AND topic_type = ?",
            (message_id, s_id, _cup_topic_type(division_id))
        )


def get_cup_stage_games(stage_id: int) -> list[dict]:
    """Сыгранные и несыгранные игры этапа со счётом — для сетки Mini App.

    Заголовки серий сюда не входят: у них нет своего счёта, итог серии живёт
    в `cup_series.team1_wins`/`team2_wins`.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT m.id AS match_id, m.cup_series_id AS series_id, m.game_num_in_series,
                   m.status, m.player1_score, m.player2_score, m.cup_winner_team
            FROM matches m
            JOIN cup_series cs ON cs.id = m.cup_series_id
            WHERE cs.stage_id = ? AND COALESCE(m.is_series_header, 0) = 0
            ORDER BY cs.series_num ASC, m.game_num_in_series ASC
            """,
            (stage_id,)
        )
        return [dict(r) for r in cursor.fetchall()]


def get_cup_series_pair(series_id: int | None) -> tuple[str, str] | None:
    """Пара клубов серии — единственный источник имён для её заголовка."""
    if series_id is None:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team1_name, team2_name FROM cup_series WHERE id = ?", (series_id,))
        row = cursor.fetchone()
    if not row:
        return None
    return row["team1_name"], row["team2_name"]


def get_cup_series(series_id: int) -> dict | None:
    """Серия кубка по id — для карточки серии в панели /cup. `division_id` и
    `season_id` — чей это кубок (из этапа серии)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT cs.id, cs.stage, cs.series_num, cs.team1_name, cs.team2_name,
                   cs.team1_wins, cs.team2_wins, cs.winner_name, cs.status, cs.stage_id,
                   st.division_id, st.season_id
            FROM cup_series cs
            LEFT JOIN cup_stages st ON st.id = cs.stage_id
            WHERE cs.id = ?
            """,
            (series_id,)
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def claim_cup_series_announcement(series_id: int) -> dict | None:
    """Забрать право объявить победителя серии в теме «Кубок» — ровно один раз
    на победителя. Возвращает серию, если объявлять надо, иначе None.

    Условный UPDATE атомарен: два одновременных подтверждения игр одной серии
    не объявят проход дважды.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE cup_series SET announced_winner = winner_name "
            "WHERE id = ? AND winner_name IS NOT NULL AND TRIM(winner_name) != '' "
            "AND (announced_winner IS NULL OR announced_winner != winner_name)",
            (series_id,)
        )
        if cursor.rowcount == 0:
            return None
    return get_cup_series(series_id)


def get_cup_series_games(series_id: int) -> list[dict]:
    """Игры одной серии по порядку — без строки-заголовка, у неё нет своего счёта."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id AS match_id, game_num_in_series, status,
                   player1_score, player2_score, cup_winner_team
            FROM matches
            WHERE cup_series_id = ? AND COALESCE(is_series_header, 0) = 0
            ORDER BY game_num_in_series ASC
            """,
            (series_id,)
        )
        return [dict(r) for r in cursor.fetchall()]


_CUP_SERIES_WRITE_SELECT = (
    "SELECT cs.id, cs.series_num, cs.stage, cs.stage_id, cs.team1_name, cs.team2_name, "
    "st.season_id "
    "FROM cup_series cs LEFT JOIN cup_stages st ON st.id = cs.stage_id "
)


def _cup_series_row(cursor, series_id: int):
    """Серия вместе со своим этапом — общий источник для записи игр и заголовка."""
    cursor.execute(_CUP_SERIES_WRITE_SELECT + "WHERE cs.id = ?", (series_id,))
    row = cursor.fetchone()
    if not row:
        raise ValueError(f"Серия кубка #{series_id} не найдена.")
    if row["stage_id"] is None:
        raise ValueError(
            f"Серия #{series_id} не привязана к этапу — заведи сетку через create_cup_series."
        )
    return row


def _insert_cup_series_match(cursor, series, game_num: int | None, is_header: bool) -> int:
    """Строка `matches` серии: её игра (`game_num`) или её заголовок.

    `division_id` — sentinel, а не NULL: NULL в этом проекте читается как
    «дивизион 1», и кубковая строка утонула бы в сетке Дивизиона 1. `round_number = -1`
    — то же соглашение, что уже используют читалки кубковых очков.

    Заголовок серии заводится БЕЗ `player1_id`/`player2_id` и без имён клубов:
    это мета-объект «серия целиком», а не игра, и связывать его с расписанием,
    «Моими матчами» и приёмкой отчётов не должен ни один читатель. Имена для
    тайла линии берутся из `cup_series` через `cup_series_id`, а 322-защита
    (`find_self_participation_match`) добирается до клубов той же связью.
    """
    if is_header:
        u1_id = u2_id = None
        t1 = t2 = None
    else:
        u1 = find_user_by_team(series["team1_name"])
        u2 = find_user_by_team(series["team2_name"])
        u1_id = u1["telegram_id"] if u1 else None
        u2_id = u2["telegram_id"] if u2 else None
        t1 = series["team1_name"]
        t2 = series["team2_name"]

    cursor.execute(
        """
        INSERT INTO matches (round_number, player1_id, player2_id, player1_team, player2_team,
                             status, division_id, season_id, tournament_type, cup_stage,
                             cup_series_id, game_num_in_series, stage_id, is_series_header)
        VALUES (-1, ?, ?, ?, ?, 'pending', ?, ?, 'cup', ?, ?, ?, ?, ?)
        """,
        (
            u1_id,
            u2_id,
            t1,
            t2,
            CUP_DIVISION_SENTINEL,
            series["season_id"],
            series["stage"],
            series["id"],
            game_num,
            series["stage_id"],
            1 if is_header else 0,
        )
    )
    return cursor.lastrowid


def create_cup_series_match(series_id: int, game_num: int = 1) -> int:
    """Завести игру серии."""
    if game_num < 1:
        raise ValueError(f"Номер игры в серии должен быть не меньше 1, получено {game_num}.")
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM matches WHERE cup_series_id = ? AND game_num_in_series = ? LIMIT 1",
            (series_id, game_num)
        )
        if cursor.fetchone():
            raise ValueError(f"Игра {game_num} серии #{series_id} уже заведена.")
        series = _cup_series_row(cursor, series_id)
        return _insert_cup_series_match(cursor, series, game_num=game_num, is_header=False)


def create_cup_series_header(series_id: int) -> int:
    """Завести (или вернуть существующую) строку-заголовок серии.

    Идемпотентность обязательна: линия этапа пересобирается при каждом показе
    страницы, и без проверки у серии плодился бы второй объект рынка на те же
    «кто проходит» и «счёт серии».
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id FROM matches WHERE cup_series_id = ? AND is_series_header = 1 LIMIT 1",
            (series_id,)
        )
        row = cursor.fetchone()
        if row:
            return int(row["id"])
        series = _cup_series_row(cursor, series_id)
        return _insert_cup_series_match(cursor, series, game_num=None, is_header=True)


# ═════════════════════════════════════════════════════════════════════════════
# 🏆 ОБЩИЙ КУБОК — ПРОГРЕСС СЕРИИ ПОСЛЕ ПОДТВЕРЖДЕНИЯ ИГРЫ
# ═════════════════════════════════════════════════════════════════════════════


def _cup_game_winner_club(match_row) -> tuple[str | None, str]:
    """Клуб, взявший игру, и способ: ('Интер Милан', 'penalties') | (None, ...).

    Решающий счёт основного времени решает игру сам: послематчевые бывают только
    после ничьей, поэтому `cup_winner_team`, спорящий со счётом 3:1, — это
    устаревший ответ (прошлая попытка отчёта, сброс матча), а не результат.
    Поле победителя читается только при равном счёте. Равный счёт без него — не
    ничья (её в кубке нет), а недообработанный результат: послематчевые нигде не
    записаны. Выдумывать победителя нельзя, поэтому вызывающий обязан получить
    ValueError и не подтверждать такую игру.
    """
    winner = (_row_col(match_row, "cup_winner_team") or "").strip()
    t1 = _row_col(match_row, "player1_team")
    t2 = _row_col(match_row, "player2_team")
    s1 = _row_col(match_row, "player1_score")
    s2 = _row_col(match_row, "player2_score")

    if s1 is not None and s2 is not None and s1 != s2:
        club = t1 if s1 > s2 else t2
        if winner and not (club and teams_match(winner, club)):
            logger.warning(
                "Cup match #%s: stale winner %r ignored, main time %s:%s decides for %r",
                _row_col(match_row, "id"), winner, s1, s2, club
            )
        return club, "main_time"

    if winner:
        if t1 and teams_match(winner, t1):
            return t1, "penalties"
        if t2 and teams_match(winner, t2):
            return t2, "penalties"
        raise ValueError(
            f"Победитель {winner!r} не играет в матче #{_row_col(match_row, 'id')}: "
            f"в паре {t1!r} и {t2!r}."
        )

    if s1 is None or s2 is None:
        return None, "pending"
    return None, "undecided"


def recompute_cup_series(cursor, series_id: int) -> dict:
    """Победы в серии по её подтверждённым играм — один пересчёт на всех вызывающих.

    Отсчёт идёт от пары `cup_series`, а не от порядка клубов в строке матча:
    жеребьёвка задаёт пару один раз, а игры серии могут быть записаны и в другом
    порядке. Способ разрешения (основное время/послематчевые) берётся у игры,
    которая довела счёт до двух побед.
    """
    cursor.execute(
        "SELECT id, player1_team, player2_team, player1_score, player2_score, cup_winner_team "
        "FROM matches WHERE cup_series_id = ? AND is_series_header = 0 AND status = 'confirmed' "
        "ORDER BY game_num_in_series ASC, id ASC",
        (series_id,)
    )
    games = cursor.fetchall()
    cursor.execute("SELECT id, team1_name, team2_name FROM cup_series WHERE id = ?", (series_id,))
    series = cursor.fetchone()
    if not series:
        raise ValueError(f"Серия кубка #{series_id} не найдена.")

    t1_name, t2_name = series["team1_name"], series["team2_name"]
    wins1 = wins2 = 0
    winner_source = None
    for g in games:
        club, source = _cup_game_winner_club(g)
        if club is None:
            if source == "undecided":
                raise ValueError(
                    f"Игра #{g['id']} серии {t1_name} — {t2_name} закончилась равным счётом, "
                    "а победитель не указан: в кубке ничьих не бывает."
                )
            continue
        if teams_match(club, t1_name):
            wins1 += 1
            if wins1 >= 2 and winner_source is None:
                winner_source = source
        elif teams_match(club, t2_name):
            wins2 += 1
            if wins2 >= 2 and winner_source is None:
                winner_source = source

    winner_name = None
    if wins1 >= 2:
        winner_name = t1_name
    elif wins2 >= 2:
        winner_name = t2_name
    return {
        "series_id": series_id,
        "team1_wins": wins1,
        "team2_wins": wins2,
        "winner_name": winner_name,
        "winner_source": winner_source,
        "decided": winner_name is not None,
    }


def _sync_cup_series_header(cursor, series_id: int, state: dict) -> str | None:
    """Привести ставки на заголовок решённой серии к её счёту: 'settled' | 'resettled' | None.

    Заголовок рассчитывается обычным `settle_match_predictions` со счётом =
    победы в серии, поэтому «проход», «счёт серии» и «третья игра» обслуживаются
    действующими правилами без новых веток.

    Рассчитанный однажды заголовок (`status = 'confirmed'`) может разойтись с
    серией: сброс игры (`reset_match`) снимает победу, и серия решается заново —
    другим счётом или другим клубом. `settle_match_predictions` трогает только
    pending-купоны и оставил бы прежние выплаты, поэтому расхождение
    пересчитывается `resettle_match_predictions` — тем же путём, что правка счёта
    лигового матча. Совпадающий счёт не трогается: повторный вызов на решённой
    серии ничего не пересчитывает.
    """
    cursor.execute(
        "SELECT id, status, player1_score, player2_score FROM matches "
        "WHERE cup_series_id = ? AND is_series_header = 1 LIMIT 1",
        (series_id,)
    )
    header = cursor.fetchone()
    if not header:
        return None
    header_id = int(header["id"])
    wins1, wins2 = state["team1_wins"], state["team2_wins"]

    if header["status"] == "confirmed":
        if (header["player1_score"], header["player2_score"]) == (wins1, wins2):
            return None
        from services.settlement_engine import resettle_match_predictions

        resettle_match_predictions(header_id, wins1, wins2, match_status="finished")
        logger.info(
            "Cup series #%s header #%s resettled: %s:%s → %s:%s", series_id, header_id,
            header["player1_score"], header["player2_score"], wins1, wins2
        )
        return "resettled"

    from services.settlement_engine import settle_match_predictions

    # `settle_match_predictions` берёт итоговый статус из строки матча:
    # подтверждённый остаётся подтверждённым, pending уходит в переданный
    # статус. Без этого заголовок серии остался бы со статусом 'finished',
    # которого среди лиговых статусов нет.
    cursor.execute("UPDATE matches SET status = 'confirmed' WHERE id = ?", (header_id,))
    settle_match_predictions(header_id, wins1, wins2, match_status="finished")
    return "settled"


def _reopen_cup_series_games(cursor, series_id: int) -> int:
    """Вернуть в расписание игры, снятые досрочным концом серии; число таких игр.

    Нужно, когда сброс игры (`reset_match`) снова делает серию нерешённой: игра 3,
    снятая после 2:0, снова может понадобиться. Её рынки остаются аннулированными —
    void финален, ставки по ней уже возвращены, — поэтому игра возвращается без
    линии, только как матч сетки. Снимает игры серии только `advance_cup_series`,
    так что каждая отменённая игра серии — его рук дело.
    """
    cursor.execute(
        "UPDATE matches SET status = 'pending', player1_score = NULL, player2_score = NULL, "
        "cup_winner_team = NULL "
        "WHERE cup_series_id = ? AND is_series_header = 0 AND status = 'cancelled'",
        (series_id,)
    )
    return cursor.rowcount


def advance_cup_series(cursor, match_id: int, actor_id: int | None = None) -> dict | None:
    """Двигать серию после подтверждения её игры; если серия решена — закрыть её.

    Вызывается на курсоре `confirm_and_finalize_match`, то есть в той же
    транзакции, что записывает счёт и рассчитывает ставки игры. Атомарность здесь
    не формальность: подтверждённая игра без движения серии — это сетка, где
    результат учтён, а в 1/32 прошёл не тот клуб.

    Закрытие серии состоит из трёх шагов, и все три обязаны пережить повторный
    вызов:
      * несыгранные игры (серия закончилась 2:0) снимаются, а их рынки
        аннулируются каноническим `void_market` — тем же путём ноги возвращаются
        по всем купонам и остаются возвращёнными;
      * строка-заголовок серии рассчитывается со счётом = победы в серии
        (`_sync_cup_series_header`), а если серия после сброса игры решилась
        другим счётом — пересчитывается;
      * повторный вызов на решённой серии с тем же счётом заголовок ещё раз не
        считает.
    """
    cursor.execute("SELECT * FROM matches WHERE id = ?", (match_id,))
    match_row = cursor.fetchone()
    if not match_row or not match_is_cup(match_row) or _row_col(match_row, "is_series_header"):
        return None
    series_id = _row_col(match_row, "cup_series_id")
    if not series_id:
        return None

    club, source = _cup_game_winner_club(match_row)
    if club is None and source == "undecided":
        raise ValueError(
            f"Игра #{match_id} закончилась со счётом "
            f"{_row_col(match_row, 'player1_score')}:{_row_col(match_row, 'player2_score')} — "
            "укажи, кто прошёл дальше (послематчевые в кубке считаются жребием)."
        )
    if club and (_row_col(match_row, "cup_winner_team") or "").strip() != club:
        # Поле победителя всегда равно тому, кто взял игру: на него опирается и
        # расчёт ставки, и пересчёт серии после сброса матча. Устаревший ответ,
        # который перебил решающий счёт, здесь же и заменяется.
        cursor.execute("UPDATE matches SET cup_winner_team = ? WHERE id = ?", (club, match_id))

    cursor.execute("SELECT winner_name FROM cup_series WHERE id = ?", (series_id,))
    before = cursor.fetchone()
    if not before:
        raise ValueError(f"Серия кубка #{series_id} не найдена.")
    was_decided = bool(before["winner_name"])

    state = recompute_cup_series(cursor, series_id)
    cursor.execute(
        "UPDATE cup_series SET team1_wins = ?, team2_wins = ?, winner_name = ?, winner_source = ?, "
        "status = ? WHERE id = ?",
        (
            state["team1_wins"], state["team2_wins"], state["winner_name"],
            state["winner_source"], "completed" if state["decided"] else "active", series_id,
        )
    )
    summary = dict(state)
    summary["voided_games"] = []
    summary["header_settled"] = False
    if not state["decided"]:
        return summary

    if not was_decided:
        cursor.execute(
            "SELECT id FROM matches WHERE cup_series_id = ? AND is_series_header = 0 "
            "AND status NOT IN ('confirmed', 'completed', 'cancelled') ORDER BY game_num_in_series",
            (series_id,)
        )
        for pending in cursor.fetchall():
            game_id = int(pending["id"])
            # 'closed' — обязательная часть выборки: старт этапа закрывает линию
            # (`_close_line_scope`), и к досрочному концу серии рынки несыгранной
            # игры уже не 'open'. Без них ставка на игру 3 висела бы в pending
            # вечно — без возврата и с занятым слотом лимита открытых купонов.
            cursor.execute(
                "SELECT id FROM markets WHERE match_id = ? AND status IN ('open', 'suspended', 'closed')",
                (game_id,)
            )
            market_ids = [int(r["id"]) for r in cursor.fetchall()]
            cursor.execute("UPDATE matches SET status = 'cancelled' WHERE id = ?", (game_id,))
            for market_id in market_ids:
                void_market(market_id, actor_id or 0, "Серия завершена досрочно — игра не была сыграна")
            summary["voided_games"].append({"match_id": game_id, "voided_markets": len(market_ids)})

    summary["header_settled"] = _sync_cup_series_header(cursor, series_id, state) is not None
    if was_decided:
        return summary

    logger.info(
        "Cup series #%s decided: %s — %s (%s), voided %s unplayed game(s)",
        series_id, state["team1_wins"], state["team2_wins"], state["winner_name"],
        len(summary["voided_games"])
    )
    return summary


def set_cup_game_winner(match_id: int, club: str, actor_id: int | None = None) -> tuple[bool, str]:
    """Записать клуб, прошедший дальше, для ещё не подтверждённой игры серии.

    Поле нужно ровно для равного счёта основного времени: послематчевых на
    скриншоте статистики нет, и без этого ответа `advance_cup_series` отказался
    бы подтверждать результат. Запись идемпотентна и не трогает сыгранный матч —
    менять прошедшего клуб можно только переигровкой (reset_match).
    """
    clean = (club or "").strip()
    if not clean:
        return False, "Не указано, кто прошёл дальше."
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, status, tournament_type, is_series_header, player1_team, player2_team, "
            "player1_score, player2_score FROM matches WHERE id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
        if not row:
            return False, f"Матч #{match_id} не найден."
        if not match_is_cup(row) or row["is_series_header"]:
            return False, "Победитель указывается только для игры общего кубка."
        if row["status"] in ("confirmed", "completed"):
            return False, "Матч уже подтверждён — смени результат через сброс матча."
        t1, t2 = row["player1_team"], row["player2_team"]
        target = t1 if (t1 and teams_match(clean, t1)) else (t2 if (t2 and teams_match(clean, t2)) else None)
        if not target:
            return False, f"{clean} не играет в матче #{match_id}."
        s1, s2 = row["player1_score"], row["player2_score"]
        if s1 is not None and s2 is not None and s1 != s2:
            # Послематчевые бывают только после ничьей: при решающем счёте
            # проход уже определён, и другой ответ был бы неправдой.
            by_score = t1 if s1 > s2 else t2
            if target != by_score:
                return False, (
                    f"Счёт основного времени {s1}:{s2} — игру взял {by_score}. "
                    "Победитель по послематчевым указывается только при равном счёте."
                )
        cursor.execute("UPDATE matches SET cup_winner_team = ? WHERE id = ?", (target, match_id))
    logger.info("Cup match #%s: winner %s declared by %s", match_id, target, actor_id)
    return True, target


def provision_cup_stage_line(
    stage: str,
    season_id: int | None = None,
    games_per_series: int = CUP_SERIES_GAMES,
    division_id: int | None = None,
) -> dict:
    """Завести этапу игры всех серий и заголовок каждой серии — одним проходом.

    Строки будущих игр существуют заранее, потому что линия открывается на этап
    целиком и закрывается его стартом: игру 2, заведённую после счёта 1:0, было бы
    уже нечем открыть для ставок (см. `constants.CUP_SERIES_GAMES`).

    Уже заведённые строки не трогаются — повторный вызов обязан давать тот же
    набор `match_id`, иначе пересчёт линии плодил бы рынки-дубли.
    """
    if games_per_series < 1:
        raise ValueError(f"В серии не может быть {games_per_series} игр.")
    s_id = _resolve_season_id(season_id)
    stage_row = get_cup_stage(stage, season_id=s_id, division_id=division_id)
    if not stage_row:
        raise ValueError(
            f"Этап кубка {cup_stage_title(cup_stage_key(stage, division_id))} не заведён — "
            "сетку строит create_cup_series."
        )

    created_games = 0
    created_headers = 0
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            _CUP_SERIES_WRITE_SELECT + "WHERE cs.stage_id = ? ORDER BY cs.series_num ASC",
            (stage_row["id"],)
        )
        series_rows = cursor.fetchall()
        for series in series_rows:
            cursor.execute(
                "SELECT game_num_in_series, is_series_header FROM matches WHERE cup_series_id = ?",
                (series["id"],)
            )
            rows = cursor.fetchall()
            have_games = {r["game_num_in_series"] for r in rows if not r["is_series_header"]}
            for game_num in range(1, games_per_series + 1):
                if game_num in have_games:
                    continue
                _insert_cup_series_match(cursor, series, game_num=game_num, is_header=False)
                created_games += 1
            if not any(r["is_series_header"] for r in rows):
                _insert_cup_series_match(cursor, series, game_num=None, is_header=True)
                created_headers += 1

    return {
        "stage": stage,
        "season_id": s_id,
        "division_id": stage_row["division_id"],
        "stage_id": stage_row["id"],
        "series": len(series_rows),
        "created_games": created_games,
        "created_headers": created_headers,
    }


# Сыгранные и отменённые матчи из линии кубка выпадают: их результат живёт в
# сетке, а в ставках им делать нечего (то же правило, что у `get_active_bet_markets`).
_CUP_LINE_HIDDEN_STATUSES = ("confirmed", "completed", "finished", "cancelled")

# Компактный набор коэффициентов тайла: (market_key, selection_key) → имя поля.
# Заголовок серии пользуется теми же полями: `p1`/`p2` там «кто проходит»,
# `tb25`/`tm25` — «будет ли третья игра» (тот же `total_goals` по счёту серии).
_CUP_TILE_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("1x2", "p1", "p1"),
    ("1x2", "p2", "p2"),
    ("total_goals", "over_2.5", "tb25"),
    ("total_goals", "under_2.5", "tm25"),
    ("btts", "btts_yes", "btts_yes"),
    ("btts", "btts_no", "btts_no"),
)


def get_cup_stage_matches(
    stage: str,
    season_id: int | None = None,
    unplayed_only: bool = False,
    division_id: int | None = None,
) -> list[dict]:
    """Плоский список матчей этапа: каждая игра каждой серии и заголовок серии.

    Клубы берутся из `cup_series`, а не из строки матча: у заголовка серии своих
    имён нет намеренно (`create_cup_series_header`), а у игры они дублируют пару.
    `stage_id`/`season_id` возвращаются вместе с матчем — по ним работает гейт и
    ценовая модель.
    """
    s_id = _resolve_season_id(season_id)
    stage_row = get_cup_stage(stage, season_id=s_id, division_id=division_id)
    if not stage_row:
        return []
    query = """
            SELECT m.id AS match_id, m.cup_series_id AS series_id, cs.series_num,
                   m.is_series_header, m.game_num_in_series, m.status,
                   m.stage_id, m.season_id,
                   cs.team1_name, cs.team2_name
            FROM matches m
            JOIN cup_series cs ON cs.id = m.cup_series_id
            WHERE cs.stage_id = ?
        """
    params: list = [stage_row["id"]]
    if unplayed_only:
        placeholders = ",".join("?" for _ in _CUP_LINE_HIDDEN_STATUSES)
        query += f" AND m.status NOT IN ({placeholders})"
        params.extend(_CUP_LINE_HIDDEN_STATUSES)
    query += " ORDER BY cs.series_num ASC, m.is_series_header DESC, m.game_num_in_series ASC"
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def get_cup_stage_line(stage: str, season_id: int | None = None, division_id: int | None = None) -> dict:
    """Линия этапа: серии с заголовками и играми и коэффициенты из реляционной схемы.

    Кубковый тайл намеренно НЕ заводится в `bet_markets`: `odd_x` там NOT NULL, а
    ничьей в кубке нет — хранить пришлось бы выдуманное число; и каждая читалка
    этой таблицы джойнит `rounds`, к которому этап отношения не имеет. Источник
    коэффициентов — `markets`/`market_selections`, та же схема, из которой Mini App
    берёт полную роспись матча. `/api/matches/{id}/markets` отдаёт её и для кубка:
    имена заголовку подставляет из `cup_series`, а на лету рынки кубку не заводит —
    лиговая модель выставила бы ему ничью.

    Сыгранные матчи из линии исключаются: их результат живёт в сетке, а не в ставках.
    """
    s_id = _resolve_season_id(season_id)
    result = {"stage": get_cup_stage(stage, season_id=s_id, division_id=division_id), "series": []}
    match_rows = get_cup_stage_matches(stage, season_id=s_id, unplayed_only=True, division_id=division_id)
    if not match_rows:
        return result

    by_series: dict[int, dict] = {}
    ordered: list[dict] = []
    tiles: list[dict] = []
    for r in match_rows:
        entry = by_series.get(r["series_id"])
        if entry is None:
            entry = {
                "series_id": r["series_id"],
                "series_num": r["series_num"],
                "team1_name": r["team1_name"],
                "team2_name": r["team2_name"],
                "header": None,
                "games": [],
            }
            by_series[r["series_id"]] = entry
            ordered.append(entry)
        tile = {
            "match_id": r["match_id"],
            "stage_id": r["stage_id"],
            "is_series_header": bool(r["is_series_header"]),
            "game_num_in_series": r["game_num_in_series"],
            "status": r["status"],
            "team1_name": r["team1_name"],
            "team2_name": r["team2_name"],
            "odds": {},
        }
        tiles.append(tile)
        if r["is_series_header"]:
            entry["header"] = tile
        else:
            entry["games"].append(tile)

    with transaction() as conn:
        cursor = conn.cursor()
        placeholders = ",".join("?" for _ in tiles)
        cursor.execute(
            f"""
            SELECT mk.match_id, mk.market_key, ms.selection_key, ms.odds_value
            FROM markets mk
            JOIN market_selections ms ON ms.market_id = mk.id
            WHERE mk.match_id IN ({placeholders})
              AND mk.status = 'open' AND ms.status = 'active'
            """,
            tuple(t["match_id"] for t in tiles)
        )
        priced = {(r["match_id"], r["market_key"], r["selection_key"]): float(r["odds_value"])
                  for r in cursor.fetchall()}

    for tile in tiles:
        odds = {}
        for market_key, selection_key, field in _CUP_TILE_FIELDS:
            value = priced.get((tile["match_id"], market_key, selection_key))
            if value is not None:
                odds[field] = round(value, 2)
        tile["odds"] = odds
        tile["is_line"] = bool(odds)

    result["series"] = ordered
    return result


def _player_folder(cursor: sqlite3.Cursor):
    """Return fold(team_name, player_name) -> (club_key, player_key, display_name).

    A footballer is one (club, player_key) pair however his name was recognized:
    the spelling is folded onto the club's squad name via `match_roster_name`,
    and a name outside the squad keeps its own normalized key. Rosters are loaded
    once per club for the lifetime of the returned function.
    """
    rosters: dict[str, list[str]] = {}

    def fold(team_name: str | None, player_name: str | None) -> tuple[str, str, str]:
        raw = (player_name or "").strip()
        team = (team_name or "").strip()
        canon = (resolve_team_name(team) or team) if team else ""
        if canon not in rosters:
            rosters[canon] = _load_club_roster(cursor, canon)
        name = match_roster_name(raw, rosters[canon]) or raw
        club_key = normalize_team_name(canon) if canon else ""
        return club_key, normalize_player_name_key(name) or name.lower(), name

    return fold


def _fold_player_rows(cursor: sqlite3.Cursor, rows, fields: tuple[str, ...]) -> list[dict]:
    """Merge aggregate rows of one footballer recorded under several spellings.

    `rows` carry `player_name`, `team_name` and the numeric `fields`, which are
    summed. The first row of a player keeps its place and its other columns, and
    its name becomes the squad spelling. Callers sort and limit afterwards: a
    LIMIT in SQL would cut a player's second spelling before it was merged.
    """
    fold = _player_folder(cursor)
    merged: dict[tuple[str, str], dict] = {}
    for row in rows:
        row = dict(row)
        club_key, player_key, name = fold(row.get("team_name"), row.get("player_name"))
        entry = merged.get((club_key, player_key))
        if entry is None:
            row["player_name"] = name
            merged[(club_key, player_key)] = row
        else:
            for f in fields:
                entry[f] = (entry.get(f) or 0) + (row.get(f) or 0)
    return list(merged.values())


def _club_player_event_totals(cursor: sqlite3.Cursor, canon: str, roster: list[str], season_id: int) -> list[dict]:
    """Season goals/assists of one club's players, each spelling folded onto its squad name.

    Events keep the name as it was recognized, so one footballer can sit under
    several spellings ('EMEGA' and 'Emegha'); summing per raw string would list
    him twice. A spelling that stands for no squad player is kept, merged only
    with spellings of the same normalized key.
    """
    cursor.execute("""
        SELECT
            me.player_name, me.team_name,
            COALESCE(SUM(CASE WHEN me.event_type = 'goal' THEN me.count ELSE 0 END), 0) AS goals,
            COALESCE(SUM(CASE WHEN me.event_type = 'assist' THEN me.count ELSE 0 END), 0) AS assists
        FROM match_events me
        JOIN matches m ON me.match_id = m.id
        WHERE m.status = 'confirmed'
          AND (m.season_id = ? OR m.season_id IS NULL)
        GROUP BY me.team_name, me.player_name
        ORDER BY MIN(me.id) ASC
    """, (season_id,))
    totals: dict[str, dict] = {}
    for r in cursor.fetchall():
        raw = (r["player_name"] or "").strip()
        if not raw or not teams_match(r["team_name"], canon):
            continue
        name = match_roster_name(raw, roster) or raw
        key = normalize_player_name_key(name) or name.lower()
        entry = totals.setdefault(key, {"player_name": name, "goals": 0, "assists": 0})
        entry["goals"] += r["goals"]
        entry["assists"] += r["assists"]
    return list(totals.values())


def get_club_card_data(team_name: str) -> dict:
    """
    Get comprehensive club profile & statistics independent of who the current manager is.

    Standings, form and player stats are those of the active season, in the
    division the club plays in: the table is only meaningful inside one division,
    and ranking a club among every club of the league put a division-5 side at
    "#54" with its matches not counted at all.
    """
    act = get_active_season()
    season_id = act["id"] if act else 1
    canon = resolve_team_name(team_name) or team_name.strip()
    division_id = get_team_division_id(canon, season_id=season_id)

    with transaction() as conn:
        cursor = conn.cursor()

        # 1. Current Manager
        cursor.execute(
            "SELECT telegram_id, username, warn_count, registered_at, team_name FROM users WHERE team_name IS NOT NULL"
        )
        all_users = cursor.fetchall()
        u_row = None
        for r in all_users:
            if teams_match(r["team_name"], canon):
                u_row = r
                break

        manager = None
        if u_row:
            manager = {
                "telegram_id": u_row["telegram_id"],
                "username": u_row["username"],
                "warn_count": u_row["warn_count"] or 0,
                "registered_at": u_row["registered_at"],
            }

        # 2. Standings & League Stats
        standings = get_standings(division_id=division_id, season_id=season_id)
        league_stats = {
            "rank": 0,
            "played": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "goals_scored": 0,
            "goals_conceded": 0,
            "goal_diff": 0,
            "points": 0,
        }
        for rank, row in enumerate(standings, 1):
            if teams_match(row["team_name"], canon):
                league_stats = {
                    "rank": rank,
                    "played": row["played"],
                    "wins": row["wins"],
                    "draws": row["draws"],
                    "losses": row["losses"],
                    "goals_scored": row["goals_scored"],
                    "goals_conceded": row["goals_conceded"],
                    "goal_diff": row["goals_scored"] - row["goals_conceded"],
                    "points": row["points"],
                }
                break

        # 3. Recent Form (Last 5 matches)
        form_map = get_teams_recent_form(limit=5, division_id=division_id, season_id=season_id)
        recent_form = form_map.get(canon.lower(), [])

        # 4. Cup Stats
        cursor.execute("""
            SELECT id, stage, series_num, team1_name, team2_name, team1_wins, team2_wins, winner_name, status
            FROM cup_series
            ORDER BY id DESC
        """)
        c_row = None
        for cr in cursor.fetchall():
            if teams_match(cr["team1_name"], canon) or teams_match(cr["team2_name"], canon):
                c_row = cr
                break

        cup_stats = None
        if c_row:
            is_t1 = teams_match(c_row["team1_name"], canon)
            opp_name = c_row["team2_name"] if is_t1 else c_row["team1_name"]
            c_wins = c_row["team1_wins"] if is_t1 else c_row["team2_wins"]
            opp_wins = c_row["team2_wins"] if is_t1 else c_row["team1_wins"]
            cup_stats = {
                "series_id": c_row["id"],
                "stage": c_row["stage"],
                "series_num": c_row["series_num"],
                "opponent": opp_name,
                "club_wins": c_wins,
                "opp_wins": opp_wins,
                "status": c_row["status"],
                "winner_name": c_row["winner_name"],
                "is_winner": bool(c_row["winner_name"] and teams_match(c_row["winner_name"], canon)),
                "is_eliminated": bool(c_row["winner_name"] and not teams_match(c_row["winner_name"], canon)),
            }

        # 5. Registered Squad
        cursor.execute(
            "SELECT player_name, team_name FROM squad_players ORDER BY id ASC"
        )
        squad_names = [r["player_name"] for r in cursor.fetchall() if teams_match(r["team_name"], canon)]

        # 6. Top Scorers and Assisters of the Club
        event_players = _club_player_event_totals(cursor, canon, squad_names, season_id)
        top_scorers = [p for p in sorted(event_players, key=lambda x: (x["goals"], x["assists"]), reverse=True) if p["goals"] > 0][:5]
        top_assists = [p for p in sorted(event_players, key=lambda x: (x["assists"], x["goals"]), reverse=True) if p["assists"] > 0][:5]

        # 7. Unplayed Matches & Debts
        cursor.execute("SELECT * FROM rounds")
        round_info_map = _load_round_states(cursor.fetchall())
        debt_rows = _load_debt_rows(cursor)

        cursor.execute("""
            SELECT 
                m.id, m.round_number, m.tournament_type, m.cup_stage, m.game_num_in_series,
                m.player1_team, m.player2_team, m.division_id, m.status, m.played_at
            FROM matches m
            WHERE m.status = 'pending'
            ORDER BY 
                CASE WHEN m.tournament_type = 'cup' OR m.round_number = -1 THEN 999 ELSE m.round_number END ASC,
                m.id ASC
        """)
        pending_matches = []
        debts_count = 0
        now_dt = now_msk()

        for pm in cursor.fetchall():
            p1_t = pm["player1_team"] or ""
            p2_t = pm["player2_team"] or ""
            if not (teams_match(p1_t, canon) or teams_match(p2_t, canon)):
                continue

            is_p1 = teams_match(p1_t, canon)
            opp = p2_t if is_p1 else p1_t
            is_cup = bool(pm["tournament_type"] == "cup" or pm["round_number"] == -1)
            
            rn = pm["round_number"]
            m_div = pm["division_id"] or 1
            r_info = round_info_map.get((m_div, rn))

            overdue = False
            if not is_cup:
                overdue = debt_policy.is_debt(dict(pm), r_info, debt_rows.get(pm["id"]), now_dt)
            else:
                # Кубок: долг, только если строка match_debts уже заведена
                overdue = pm["id"] in debt_rows

            if overdue:
                debts_count += 1

            pending_matches.append({
                "match_id": pm["id"],
                "round_number": pm["round_number"],
                "tournament_type": pm["tournament_type"],
                "cup_stage": pm["cup_stage"],
                "game_num": pm["game_num_in_series"],
                "opponent": opp,
                "is_overdue": overdue,
                "deadline": r_info.get("deadline_str") if r_info else None,
            })

        return {
            "team_name": canon,
            "division_id": division_id,
            "manager": manager,
            "league_stats": league_stats,
            "recent_form": recent_form,
            "cup_stats": cup_stats,
            "top_scorers": top_scorers,
            "top_assists": top_assists,
            "squad_names": squad_names,
            "squad_count": len(squad_names),
            "pending_matches": pending_matches,
            "debts_count": debts_count,
        }


def get_club_squad_stats(team_name: str) -> list[dict]:
    """
    Get full list of squad players for a club with their individual goal and assist stats.

    Stats are the active season's, the same numbers the club card shows.
    """
    act = get_active_season()
    season_id = act["id"] if act else 1
    with transaction() as conn:
        cursor = conn.cursor()
        canon = resolve_team_name(team_name) or team_name.strip()

        cursor.execute(
            "SELECT player_name, team_name FROM squad_players ORDER BY id ASC"
        )
        squad_names = [r["player_name"] for r in cursor.fetchall() if teams_match(r["team_name"], canon)]

        stats_map = {
            p["player_name"]: p
            for p in _club_player_event_totals(cursor, canon, squad_names, season_id)
        }

        result = []
        seen = set()
        for p_name in squad_names:
            seen.add(p_name)
            p_stat = stats_map.get(p_name, {"goals": 0, "assists": 0})
            result.append({
                "player_name": p_name,
                "goals": p_stat["goals"],
                "assists": p_stat["assists"],
                "points": p_stat["goals"] + p_stat["assists"],
                "is_registered": True,
            })

        for p_name, p_stat in stats_map.items():
            if p_name not in seen:
                result.append({
                    "player_name": p_name,
                    "goals": p_stat["goals"],
                    "assists": p_stat["assists"],
                    "points": p_stat["goals"] + p_stat["assists"],
                    "is_registered": False,
                })

        return sorted(result, key=lambda x: (x["goals"], x["assists"], x["player_name"]), reverse=True)


def get_club_match_history(team_name: str, limit: int = 20) -> list[dict]:
    """
    Retrieve chronological match history for a club (League + Cup).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        canon = resolve_team_name(team_name) or team_name.strip()

        cursor.execute("""
            SELECT 
                m.id, m.round_number, m.tournament_type, m.cup_stage, m.cup_series_id, m.game_num_in_series,
                m.player1_team, m.player2_team, m.player1_score, m.player2_score, m.status,
                u1.username AS p1_username, u2.username AS p2_username
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE m.status = 'confirmed' AND (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?))
            ORDER BY 
                CASE WHEN m.tournament_type = 'cup' OR m.round_number = -1 THEN 999 ELSE m.round_number END DESC,
                m.id DESC
            LIMIT ?
        """, (canon, canon, limit))

        matches = []
        for r in cursor.fetchall():
            is_p1 = teams_match(r["player1_team"], canon)
            club_score = r["player1_score"] if is_p1 else r["player2_score"]
            opp_score = r["player2_score"] if is_p1 else r["player1_score"]
            opp_team = r["player2_team"] if is_p1 else r["player1_team"]
            opp_user = r["p2_username"] if is_p1 else r["p1_username"]
            club_user = r["p1_username"] if is_p1 else r["p2_username"]

            if club_score > opp_score: outcome = "W"
            elif club_score < opp_score: outcome = "L"
            else: outcome = "D"

            is_cup = bool(r["tournament_type"] == "cup" or r["round_number"] == -1 or (r["cup_series_id"] and r["cup_series_id"] > 0))

            cursor.execute("""
                SELECT player_name, count 
                FROM match_events 
                WHERE match_id = ? AND LOWER(team_name) = LOWER(?) AND event_type = 'goal'
            """, (r["id"], canon))
            scorers = [f"{g['player_name']} ({g['count']})" if g['count'] > 1 else g['player_name'] for g in cursor.fetchall()]

            matches.append({
                "match_id": r["id"],
                "round_number": r["round_number"],
                "tournament_type": r["tournament_type"],
                "cup_stage": r["cup_stage"],
                "game_num": r["game_num_in_series"],
                "is_cup": is_cup,
                "opponent_team": opp_team,
                "opponent_username": opp_user,
                "club_username": club_user,
                "club_score": club_score,
                "opponent_score": opp_score,
                "outcome": outcome,
                "scorers": scorers,
            })

        return matches


def get_club_schedule_and_results(team_name: str, limit: int = 25) -> dict:
    """
    Retrieve chronological match schedule (both played results and upcoming/pending matches) for a club.
    Aggregates cup series so that 1 row = 1 cup stage/series.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        canon = resolve_team_name(team_name) or team_name.strip()

        # 1. Cup items (legacy cup disabled for Season 2)
        cup_items = []

        # 2. Fetch League Matches for this club grouped by round_number
        cursor.execute("""
            SELECT 
                m.id, m.round_number, m.tournament_type,
                m.player1_team, m.player2_team, m.player1_score, m.player2_score, m.status,
                r.is_open, r.deadline,
                u1.username AS p1_username, u2.username AS p2_username
            FROM matches m
            LEFT JOIN rounds r ON m.round_number = r.round_number
                AND COALESCE(m.division_id, 1) = COALESCE(r.division_id, 1)
                AND (m.season_id = r.season_id OR m.season_id IS NULL)
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE (m.tournament_type IS NULL OR m.tournament_type = 'league' OR m.tournament_type = '')
              AND (m.round_number IS NOT NULL AND m.round_number > 0)
              AND (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?))
              AND (r.is_open = 1 OR m.status IN ('confirmed', 'completed'))
            ORDER BY m.round_number ASC, m.id ASC
        """, (canon, canon))
        league_rows = [dict(r) for r in cursor.fetchall()]

        # Group by round_number
        rounds_dict = {}
        for r in league_rows:
            rn = r["round_number"]
            rounds_dict.setdefault(rn, []).append(r)

        league_items = []
        for rn, r_matches in rounds_dict.items():
            first_m = r_matches[0]
            is_p1 = teams_match(first_m["player1_team"], canon)
            home_team = resolve_team_name(first_m["player1_team"]) or first_m["player1_team"]
            away_team = resolve_team_name(first_m["player2_team"]) or first_m["player2_team"]
            opp_team = away_team if is_p1 else home_team
            opp_user = first_m["p2_username"] if is_p1 else first_m["p1_username"]

            tour_title = f"ЛИГА • ТУР {rn}"

            # Collect match events / goals for all matches in this round
            m_ids = [m["id"] for m in r_matches]
            placeholders = ",".join(["?"] * len(m_ids))
            cursor.execute(f"""
                SELECT me.player_name, SUM(me.count) AS cnt
                FROM match_events me
                WHERE me.match_id IN ({placeholders}) AND LOWER(me.team_name) = LOWER(?) AND me.event_type = 'goal'
                GROUP BY LOWER(me.player_name)
            """, (*m_ids, canon))
            goals = [{"player_name": g["player_name"], "team_name": canon, "cnt": g["cnt"]}
                     for g in cursor.fetchall()]
            goals = _fold_player_rows(cursor, goals, ("cnt",))
            goals.sort(key=lambda g: (-g["cnt"], g["player_name"]))
            club_scorers = [f"{g['player_name']} ({g['cnt']})" if g['cnt'] > 1 else g['player_name'] for g in goals]

            confirmed_matches = [m for m in r_matches if m["status"] == "confirmed" and m["player1_score"] is not None and m["player2_score"] is not None]
            has_played = len(confirmed_matches) > 0
            all_confirmed = len(confirmed_matches) == len(r_matches)

            if len(r_matches) == 1:
                m = r_matches[0]
                h_score = m["player1_score"] if m["status"] == "confirmed" else None
                a_score = m["player2_score"] if m["status"] == "confirmed" else None
                if m["status"] == "confirmed" and h_score is not None and a_score is not None:
                    c_s = m["player1_score"] if is_p1 else m["player2_score"]
                    o_s = m["player2_score"] if is_p1 else m["player1_score"]
                    if c_s > o_s: outcome = "W"
                    elif c_s < o_s: outcome = "L"
                    else: outcome = "D"
                    subline = f"Голы клуба: {', '.join(club_scorers)}" if club_scorers else ""
                else:
                    outcome = "PENDING"
                    subline = ""
            else:
                games_scores = [f"{m['player1_score']}:{m['player2_score']}" for m in confirmed_matches]
                tot_p1 = sum(m["player1_score"] for m in confirmed_matches)
                tot_p2 = sum(m["player2_score"] for m in confirmed_matches)
                h_score = tot_p1 if has_played else None
                a_score = tot_p2 if has_played else None

                if has_played:
                    c_s = tot_p1 if is_p1 else tot_p2
                    o_s = tot_p2 if is_p1 else tot_p1
                    if c_s > o_s: outcome = "W"
                    elif c_s < o_s: outcome = "L"
                    else: outcome = "D"
                else:
                    outcome = "PENDING"

                games_str = ", ".join(games_scores)
                if games_str and club_scorers:
                    subline = f"Матчи: {games_str} • Голы: {', '.join(club_scorers)}"
                elif games_str:
                    subline = f"Матчи: {games_str}"
                elif club_scorers:
                    subline = f"Голы клуба: {', '.join(club_scorers)}"
                else:
                    subline = ""

            league_items.append({
                "match_id": first_m["id"],
                "round_number": rn,
                "stage_order": 0,
                "tour_title": tour_title,
                "is_cup": False,
                "home_team": home_team,
                "away_team": away_team,
                "home_score": h_score,
                "away_score": a_score,
                "club_score": h_score if is_p1 else a_score,
                "opponent_score": a_score if is_p1 else h_score,
                "is_home": is_p1,
                "opponent_team": opp_team,
                "opponent_username": opp_user,
                "status": "confirmed" if (has_played and all_confirmed) else "pending",
                "outcome": outcome,
                "scorers": club_scorers,
                "subline": subline,
                "is_completed": all_confirmed and has_played,
                "has_played": has_played,
            })

        # 3. Combine and Sort:
        played_league = [l for l in league_items if l["is_completed"]]
        played_league.sort(key=lambda x: -x["round_number"]) # Recent rounds first

        pending_league = [l for l in league_items if not l["is_completed"]]
        pending_league.sort(key=lambda x: x["round_number"]) # Nearest upcoming round first

        all_items = played_league + pending_league

        # Calculate counts
        played_count = len(played_league)
        pending_count = len(pending_league)

        return {
            "team_name": canon,
            "played_count": played_count,
            "pending_count": pending_count,
            "matches": all_items[:limit],
        }


def get_all_clubs_summary() -> list[dict]:
    """
    Get summary list of every league club for the clubs catalog.

    Seeded from config.CLUB_REGISTRY (all five divisions) so that a club shows up
    in the catalog before its coach registers, plus any registered club missing
    from the registry — that one is a drift signal, not a reason to hide it.
    """
    from config import CLUB_REGISTRY
    standings = get_standings()
    form_map = get_teams_recent_form(limit=5)
    
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id, username, team_name, warn_count FROM users WHERE team_name IS NOT NULL AND team_name != ''")
        users = {resolve_team_name(u["team_name"]).lower(): dict(u) for u in cursor.fetchall() if resolve_team_name(u["team_name"])}

    standings_map = {resolve_team_name(s["team_name"]).lower(): (rank, s) for rank, s in enumerate(standings, 1) if resolve_team_name(s["team_name"])}

    catalog: list[str] = list(CLUB_REGISTRY)
    known = {resolve_team_name(t).lower() for t in catalog if resolve_team_name(t)}
    for u in users.values():
        canon = resolve_team_name(u["team_name"]) or u["team_name"]
        if canon.lower() not in known:
            known.add(canon.lower())
            catalog.append(canon)

    result = []
    for t in catalog:
        canon = resolve_team_name(t) or t
        canon_lower = canon.lower()
        
        rank, s_row = standings_map.get(canon_lower, (0, {"played": 0, "points": 0, "wins": 0, "draws": 0, "losses": 0}))
        u_info = users.get(canon_lower)
        form = form_map.get(canon_lower, [])

        result.append({
            "team_name": canon,
            "rank": rank,
            "manager_username": u_info.get("username") if u_info else None,
            "manager_id": u_info.get("telegram_id") if u_info else None,
            "warn_count": u_info.get("warn_count", 0) if u_info else 0,
            "played": s_row["played"],
            "points": s_row["points"],
            "wins": s_row["wins"],
            "draws": s_row["draws"],
            "losses": s_row["losses"],
            "recent_form": form,
        })

    return sorted(result, key=lambda x: (x["rank"] if x["rank"] > 0 else 999, -x["points"], x["team_name"]))


def get_division_teams(division_id: int, season_id: int | None = None) -> list[str]:
    """
    Retrieve unique canonical team names belonging to a specific division.

    Three sources: the division's seeded roster in config.DIVISION_CLUBS (keyed
    by divisions.code), the coaches registered in the division, and the clubs of
    matches scheduled in it. The seed is what makes a club selectable *before*
    anyone owns it — without it a fresh season has no clubs at all, so an admin
    could never bind the first participant to one.
    """
    from config import DIVISION_CLUBS

    target_season_id = season_id
    if target_season_id is None:
        act = get_active_season()
        target_season_id = act["id"] if act else 1

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT code FROM divisions WHERE id = ?", (division_id,))
        div_row = cursor.fetchone()
        div_code = (div_row["code"] or "").strip().upper() if div_row else ""
        seeded_teams = list(DIVISION_CLUBS.get(div_code, []))

        cursor.execute(
            "SELECT DISTINCT team_name FROM users WHERE division_id = ? AND team_name IS NOT NULL AND team_name != ''",
            (division_id,)
        )
        user_teams = [resolve_team_name(r["team_name"]) or r["team_name"].strip() for r in cursor.fetchall() if r["team_name"]]

        cursor.execute("""
            SELECT DISTINCT player1_team FROM matches WHERE division_id = ? AND (season_id = ? OR season_id IS NULL) AND player1_team IS NOT NULL
            UNION
            SELECT DISTINCT player2_team FROM matches WHERE division_id = ? AND (season_id = ? OR season_id IS NULL) AND player2_team IS NOT NULL
        """, (division_id, target_season_id, division_id, target_season_id))
        match_teams = [resolve_team_name(r[0]) or r[0].strip() for r in cursor.fetchall() if r[0]]

    seen = set()
    result = []
    for t in seeded_teams + user_teams + match_teams:
        if t:
            t_low = t.lower()
            if t_low not in seen:
                seen.add(t_low)
                result.append(t)
    return sorted(result)


def get_division_squads_status(division_id: int) -> dict:
    """
    Calculate squad upload status for all clubs in a division.
    Returns status for each club (ready, partial, empty, vacant) and aggregate metrics.
    """
    division = get_division(division_id)
    div_name = division.get("name") if division else f"Дивизион #{division_id}"
    div_code = (division.get("code") or "").strip().upper() if division else ""

    clubs = get_division_teams(division_id)
    if not clubs and div_code:
        from config import DIVISION_CLUBS
        clubs = sorted(list(DIVISION_CLUBS.get(div_code, [])))

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT telegram_id, username, team_name FROM users WHERE division_id = ?",
            (division_id,)
        )
        users = [dict(r) for r in cursor.fetchall()]

        club_to_user: dict[str, dict] = {}
        for u in users:
            u_team = u.get("team_name")
            if not u_team:
                continue
            resolved = resolve_team_name(u_team) or u_team.strip()
            club_to_user[resolved.lower()] = u
            for c in clubs:
                if teams_match(u_team, c):
                    club_to_user[c.lower()] = u

        cursor.execute("SELECT team_name, COUNT(*) as cnt FROM squad_players GROUP BY team_name")
        squad_counts_raw = {r["team_name"]: r["cnt"] for r in cursor.fetchall()}

        def _get_player_count(club_name: str) -> int:
            if club_name in squad_counts_raw:
                return squad_counts_raw[club_name]
            club_low = club_name.lower()
            for t_name, cnt in squad_counts_raw.items():
                if t_name.lower() == club_low or teams_match(t_name, club_name):
                    return cnt
            return 0

        club_details = []
        ready_count = 0
        partial_count = 0
        empty_count = 0
        vacant_count = 0

        for club in clubs:
            user = club_to_user.get(club.lower())
            player_count = _get_player_count(club)

            if not user or not user.get("telegram_id"):
                status = "vacant"
                vacant_count += 1
                user_id = None
                username = None
            elif player_count >= 11:
                status = "ready"
                ready_count += 1
                user_id = user["telegram_id"]
                username = user.get("username")
            elif player_count > 0:
                status = "partial"
                partial_count += 1
                user_id = user["telegram_id"]
                username = user.get("username")
            else:
                status = "empty"
                empty_count += 1
                user_id = user["telegram_id"]
                username = user.get("username")

            club_details.append({
                "club": club,
                "user_id": user_id,
                "username": username,
                "player_count": player_count,
                "status": status,
            })

    total = len(clubs)
    return {
        "division_id": division_id,
        "division_name": div_name,
        "division_code": div_code,
        "total_clubs": total,
        "ready_count": ready_count,
        "partial_count": partial_count,
        "empty_count": empty_count,
        "vacant_count": vacant_count,
        "uploaded_count": ready_count,
        "clubs": club_details,
    }


def get_all_divisions_squads_summary() -> dict:
    """
    Calculate squad upload status across all active divisions.
    """
    divisions = get_divisions(is_active=True)
    summaries = []
    total_clubs = 0
    total_ready = 0
    total_partial = 0
    total_empty = 0
    total_vacant = 0

    for div in divisions:
        div_stat = get_division_squads_status(div["id"])
        summaries.append(div_stat)
        total_clubs += div_stat["total_clubs"]
        total_ready += div_stat["ready_count"]
        total_partial += div_stat["partial_count"]
        total_empty += div_stat["empty_count"]
        total_vacant += div_stat["vacant_count"]

    return {
        "total_clubs": total_clubs,
        "total_ready": total_ready,
        "total_partial": total_partial,
        "total_empty": total_empty,
        "total_vacant": total_vacant,
        "divisions": summaries,
    }


def get_team_division_id(team_name: str, season_id: int | None = None) -> int | None:
    """
    Determine which division a club currently plays in.

    The manager's registration is the primary source; scheduled matches are the
    fallback for clubs without an assigned manager. Returns None when the club
    cannot be attributed to any division.
    """
    if not team_name:
        return None

    canon = resolve_team_name(team_name) or team_name.strip()
    if not canon:
        return None

    target_season_id = season_id
    if target_season_id is None:
        act = get_active_season()
        target_season_id = act["id"] if act else 1

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT division_id, team_name FROM users "
            "WHERE division_id IS NOT NULL AND team_name IS NOT NULL AND team_name != ''"
        )
        for row in cursor.fetchall():
            if teams_match(row["team_name"], canon):
                return int(row["division_id"])

        cursor.execute("""
            SELECT division_id, COUNT(*) AS n
            FROM matches
            WHERE division_id IS NOT NULL
              AND (season_id = ? OR season_id IS NULL)
              AND (player1_team = ? OR player2_team = ?)
            GROUP BY division_id
            ORDER BY n DESC
            LIMIT 1
        """, (target_season_id, canon, canon))
        row = cursor.fetchone()
        return int(row["division_id"]) if row else None


def get_clubs_summary_for_division(division_id: int, season_id: int | None = None) -> list[dict]:
    """
    Get summary list of clubs for a specific division for the clubs catalog.
    """
    target_season_id = season_id
    if target_season_id is None:
        act = get_active_season()
        target_season_id = act["id"] if act else 1

    teams = get_division_teams(division_id, season_id=target_season_id)
    standings = get_standings(division_id=division_id, season_id=target_season_id)
    form_map = get_teams_recent_form(limit=5, division_id=division_id, season_id=target_season_id)

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_id, username, team_name, warn_count 
            FROM users 
            WHERE division_id = ? AND team_name IS NOT NULL AND team_name != ''
        """, (division_id,))
        users = {resolve_team_name(u["team_name"]).lower(): dict(u) for u in cursor.fetchall() if resolve_team_name(u["team_name"])}

    standings_map = {resolve_team_name(s["team_name"]).lower(): (rank, s) for rank, s in enumerate(standings, 1) if resolve_team_name(s["team_name"])}

    result = []
    for t in teams:
        canon = resolve_team_name(t) or t
        canon_lower = canon.lower()

        rank, s_row = standings_map.get(canon_lower, (0, {"played": 0, "points": 0, "wins": 0, "draws": 0, "losses": 0}))
        u_info = users.get(canon_lower)
        form = form_map.get(canon_lower, [])

        result.append({
            "team_name": canon,
            "rank": rank,
            "manager_username": u_info.get("username") if u_info else None,
            "manager_id": u_info.get("telegram_id") if u_info else None,
            "warn_count": u_info.get("warn_count", 0) if u_info else 0,
            "played": s_row["played"],
            "points": s_row["points"],
            "wins": s_row["wins"],
            "draws": s_row["draws"],
            "losses": s_row["losses"],
            "recent_form": form,
        })

    return sorted(result, key=lambda x: (x["rank"] if x["rank"] > 0 else 999, -x["points"], x["team_name"]))


def rename_player(old_name: str, new_name: str, team_name: str | None = None) -> tuple[int, int]:
    """
    Rename a player across squad_players, match_events, and matches.mvp_player.
    Returns (squad_updated_count, events_updated_count).
    """
    old_clean = old_name.strip()
    new_clean = new_name.strip()
    new_norm = normalize_player_name_key(new_clean)
    with transaction() as conn:
        cursor = conn.cursor()
        if team_name:
            t_clean = team_name.strip()
            cursor.execute(
                "UPDATE squad_players SET player_name = ?, norm_name = ? WHERE LOWER(player_name) = LOWER(?) AND LOWER(team_name) = LOWER(?)",
                (new_clean, new_norm, old_clean, t_clean)
            )
            c1 = cursor.rowcount
            cursor.execute(
                "UPDATE match_events SET player_name = ? WHERE LOWER(player_name) = LOWER(?) AND LOWER(team_name) = LOWER(?)",
                (new_clean, old_clean, t_clean)
            )
            c2 = cursor.rowcount
            cursor.execute(
                "UPDATE matches SET mvp_player = ? WHERE LOWER(mvp_player) = LOWER(?) AND (LOWER(player1_team) = LOWER(?) OR LOWER(player2_team) = LOWER(?))",
                (new_clean, old_clean, t_clean, t_clean)
            )
        else:
            cursor.execute(
                "UPDATE squad_players SET player_name = ?, norm_name = ? WHERE LOWER(player_name) = LOWER(?)",
                (new_clean, new_norm, old_clean)
            )
            c1 = cursor.rowcount
            cursor.execute(
                "UPDATE match_events SET player_name = ? WHERE LOWER(player_name) = LOWER(?)",
                (new_clean, old_clean)
            )
            c2 = cursor.rowcount
            cursor.execute(
                "UPDATE matches SET mvp_player = ? WHERE LOWER(mvp_player) = LOWER(?)",
                (new_clean, old_clean)
            )
        return (c1, c2)


def parse_flexible_datetime(dt_str: str | None) -> datetime.datetime | None:
    """Parse date/datetime string supporting multiple formats commonly used in the league."""
    if not dt_str or not str(dt_str).strip():
        return None
    s = str(dt_str).strip()
    formats = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y",
        "%Y-%m-%d",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(s, fmt)
        except ValueError:
            continue

    # Try format without year (e.g. "20.08 12:00" or "20.08") -> use current year
    try:
        now_year = now_msk().year
        return datetime.datetime.strptime(f"{s}.{now_year}", "%d.%m %H:%M.%Y")
    except ValueError:
        pass
    try:
        now_year = now_msk().year
        return datetime.datetime.strptime(f"{s}.{now_year}", "%d.%m.%Y")
    except ValueError:
        pass
    return None


def get_all_unplayed_league_matches(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Несыгранные матчи лиги, которые сейчас долг (см. `services.debt_policy`)."""
    matches = get_detailed_overdue_matches(division_id=division_id, season_id=season_id)
    for m in matches:
        m["p1_team"] = m.get("player1_team")
        m["p2_team"] = m.get("player2_team")
    return matches


def _load_round_states(rounds_rows) -> dict[tuple[int, int], dict]:
    """Строки туров по ключу (дивизион, номер тура).

    Номер тура сам по себе не идентифицирует тур: 1-й тур есть в каждом дивизионе.
    `division_id = NULL` означает дивизион 1 — то же соглашение, что в
    `place_user_bet`. Строка с явным сезоном перекрывает легаси-строку без сезона.
    Значения — полные строки `rounds` (dict), их читает `services.debt_policy`.
    """
    round_map: dict[tuple[int, int], dict] = {}
    for row in rounds_rows:
        r = dict(row)
        key = (r.get("division_id") or 1, r["round_number"])
        if key in round_map and r.get("season_id") is None and round_map[key].get("season_id") is not None:
            continue
        r["deadline_str"] = r.get("deadline")
        r["deadline_dt"] = debt_policy.round_deadline(r)
        round_map[key] = r
    return round_map


def _fetch_round_rows(cursor, season_id: int, division_id: int | None = None):
    if division_id is not None:
        cursor.execute(
            "SELECT * FROM rounds WHERE (season_id = ? OR season_id IS NULL) AND COALESCE(division_id, 1) = ?",
            (season_id, division_id)
        )
    else:
        cursor.execute("SELECT * FROM rounds WHERE (season_id = ? OR season_id IS NULL)", (season_id,))
    return cursor.fetchall()


def _load_debt_rows(cursor, match_ids=None) -> dict[int, dict]:
    """Строки `match_debts` (кроме снятых) по match_id; пусто, пока таблицы нет.

    Таблица маленькая — не больше числа матчей сезона, поэтому фильтр по
    `match_ids` идёт в Python, а не через IN-список в тексте запроса.
    """
    try:
        cursor.execute("SELECT * FROM match_debts WHERE state != 'cancelled'")
    except sqlite3.OperationalError:
        return {}
    rows = {r["match_id"]: dict(r) for r in cursor.fetchall()}
    if match_ids is not None:
        wanted = {int(i) for i in match_ids}
        rows = {k: v for k, v in rows.items() if k in wanted}
    return rows



# ─── Долги матчей: таблица match_debts ────────────────────────────────────
#
# Политика («долг или нет, и с какого момента») живёт в services.debt_policy.
# Здесь — только хранение: строка появляется, когда матч становится долгом
# (закрытие тура или синхронизация после дедлайна), и хранит срок, стадии
# трекера и итог. Состояния: active / escalated / resolved / cancelled.
# `cancelled` — долг снят переносом дедлайна или повторным открытием тура;
# такая строка не считается долгом и может ожить при новом дедлайне.

_DEBT_TS = "%Y-%m-%d %H:%M:%S"
_DEBT_OPEN_STATES = ("active", "escalated")


def _ts(value: datetime.datetime | None) -> str | None:
    return value.strftime(_DEBT_TS) if value else None


def _upsert_debt_row(cursor, match: dict, terms: debt_policy.DebtTerms, now: datetime.datetime) -> bool:
    """Завести долг матча. Живую строку не трогает; снятую (`cancelled`) — оживляет."""
    cursor.execute("""
        INSERT INTO match_debts (
            match_id, division_id, season_id, round_number,
            became_debt_at, grace_hours, escalate_at, state, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
        ON CONFLICT(match_id) DO UPDATE SET
            division_id = excluded.division_id,
            season_id = excluded.season_id,
            round_number = excluded.round_number,
            became_debt_at = excluded.became_debt_at,
            grace_hours = excluded.grace_hours,
            escalate_at = excluded.escalate_at,
            state = 'active',
            last_reminder_at = NULL,
            soft_warned_at = NULL,
            escalated_at = NULL,
            last_escalation_at = NULL,
            escalation_count = 0,
            global_escalated_at = NULL,
            resolved_at = NULL,
            resolution = NULL,
            resolved_by = NULL,
            created_at = excluded.created_at
        WHERE match_debts.state = 'cancelled'
    """, (
        match["id"], match.get("division_id") or 1, match.get("season_id"), match.get("round_number"),
        _ts(terms.became_debt_at), terms.grace_hours, _ts(terms.escalate_at), _ts(now),
    ))
    return cursor.rowcount > 0


def _snapshot_legacy_debts(cursor, now: datetime.datetime) -> int:
    """Миграция 020: перенести долги из флагов debt_reminders в match_debts.

    Несыгранный матч лиги — долг, если дедлайн его тура прошёл (старое правило
    без случая «открытый тур без дедлайна»). Сыгранные и кубковые матчи
    переносятся, только если у них уже есть стадии: там записаны выданные
    награды и вердикты, повторять которые нельзя.
    """
    try:
        cursor.execute("SELECT match_id, stage, sent_at FROM debt_reminders")
        stage_rows = cursor.fetchall()
    except sqlite3.OperationalError:
        stage_rows = []
    stages: dict[int, dict[str, str | None]] = {}
    for r in stage_rows:
        stages.setdefault(r["match_id"], {})[r["stage"]] = r["sent_at"]

    cursor.execute("SELECT * FROM rounds")
    rounds_by_key: dict[tuple[int, int], list[dict]] = {}
    for r in cursor.fetchall():
        r = dict(r)
        rounds_by_key.setdefault((r.get("division_id") or 1, r["round_number"]), []).append(r)

    cursor.execute(
        "SELECT id, round_number, division_id, season_id, status, played_at, tournament_type FROM matches"
    )
    migrated = 0
    for m in [dict(r) for r in cursor.fetchall()]:
        st = stages.get(m["id"], {})
        is_cup = m.get("tournament_type") == "cup" or m["round_number"] == -1
        deadline = None
        if not is_cup:
            cands = rounds_by_key.get((m.get("division_id") or 1, m["round_number"]), [])
            r_row = next((r for r in cands if r.get("season_id") == m.get("season_id")), None) or next(
                (r for r in cands if r.get("season_id") is None or m.get("season_id") is None), None
            )
            deadline = debt_policy.round_deadline(r_row)
        pending = m["status"] == "pending"
        if pending and not is_cup:
            if deadline is None or deadline > now:
                continue
        elif not st:
            continue

        became = deadline if deadline is not None and deadline <= now else None
        if became is None:
            sent = [d for d in (parse_flexible_datetime(v) for v in st.values() if v) if d]
            became = min(sent) if sent else now
        terms = debt_policy.terms_for(became)

        escalated = st.get("admin_escalated_48h")
        if not pending:
            state = "resolved"
        elif escalated:
            state = "escalated"
        else:
            state = "active"
        cursor.execute("""
            INSERT OR IGNORE INTO match_debts (
                match_id, division_id, season_id, round_number,
                became_debt_at, grace_hours, escalate_at, state,
                last_reminder_at, soft_warned_at, escalated_at, last_escalation_at, escalation_count,
                resolved_at, resolution, verdict_applied_at, reward_given_at, created_at
            )
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            m["id"], m.get("division_id") or 1, m.get("season_id"), m["round_number"],
            _ts(terms.became_debt_at), _ts(terms.escalate_at), state,
            st.get("cycle_reminder_last") or st.get("deadline_passed"),
            st.get("warn_24h"),
            escalated, escalated, 1 if escalated else 0,
            (m.get("played_at") or _ts(now)) if not pending else None,
            "played" if not pending else None,
            st.get("verdict_processed"),
            st.get("reward_given"),
            _ts(now),
        ))
        migrated += cursor.rowcount
    return migrated


def sync_match_debts(now: datetime.datetime | None = None, season_id: int | None = None) -> dict:
    """Привести match_debts в соответствие с политикой — зовёт трекер долгов.

    Заводит строки для несыгранных матчей, чей тур прошёл дедлайн, и закрывает
    (`resolved`, `played`) живые строки матчей, результат которых подтверждён.
    Идемпотентна: повторный вызов ничего не меняет.
    """
    now = now or now_msk()
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        rounds = _load_round_states(_fetch_round_rows(cursor, s_id))
        cursor.execute("""
            SELECT id, round_number, division_id, season_id, status
            FROM matches
            WHERE status = 'pending'
              AND (tournament_type IS NULL OR tournament_type = 'league')
              AND (season_id = ? OR season_id IS NULL)
        """, (s_id,))
        matches = [dict(r) for r in cursor.fetchall()]
        existing = _load_debt_rows(cursor)
        created = 0
        for m in matches:
            if m["id"] in existing:
                continue
            terms = debt_policy.debt_terms(rounds.get((m.get("division_id") or 1, m["round_number"])), now)
            if terms is not None and _upsert_debt_row(cursor, m, terms, now):
                created += 1

        cursor.execute("""
            UPDATE match_debts SET state = 'resolved', resolution = 'played', resolved_at = ?
            WHERE state IN ('active', 'escalated')
              AND match_id IN (SELECT id FROM matches WHERE status = 'confirmed')
        """, (_ts(now),))
        resolved = cursor.rowcount
    return {"created": created, "resolved": resolved}


def get_match_debt(match_id: int) -> dict | None:
    """Строка match_debts матча (включая снятую) или None."""
    with transaction() as conn:
        row = conn.execute("SELECT * FROM match_debts WHERE match_id = ?", (match_id,)).fetchone()
        return dict(row) if row else None


# Отметки трекера долгов. SQL фиксирован на каждый этап — никакой подстановки
# имён колонок. Отметка ставится, только если строка долга ещё жива.
_DEBT_MARK_SQL = {
    "reminded": (
        "UPDATE match_debts SET last_reminder_at = ? "
        "WHERE match_id = ? AND state IN ('active', 'escalated')"
    ),
    "soft_warned": (
        "UPDATE match_debts SET soft_warned_at = ?, last_reminder_at = ? "
        "WHERE match_id = ? AND state IN ('active', 'escalated') AND soft_warned_at IS NULL"
    ),
    "escalated": (
        "UPDATE match_debts SET state = 'escalated', escalated_at = ?, last_escalation_at = ?, "
        "escalation_count = COALESCE(escalation_count, 0) + 1 "
        "WHERE match_id = ? AND state = 'active' AND escalated_at IS NULL"
    ),
    "reescalated": (
        "UPDATE match_debts SET last_escalation_at = ?, "
        "escalation_count = COALESCE(escalation_count, 0) + 1 "
        "WHERE match_id = ? AND state = 'escalated'"
    ),
    "global_escalated": (
        "UPDATE match_debts SET global_escalated_at = ? "
        "WHERE match_id = ? AND state = 'escalated' AND global_escalated_at IS NULL"
    ),
}
_DEBT_MARK_ARITY = {"soft_warned": 2, "escalated": 2}


def mark_debt_stage(match_id: int, stage: str, now: datetime.datetime | None = None) -> bool:
    """Отметить выполненный этап долга: reminded / soft_warned / escalated / reescalated / global_escalated.

    Возвращает True, если строка изменилась (повтор того же этапа — False).
    """
    sql = _DEBT_MARK_SQL.get(stage)
    if sql is None:
        raise ValueError(f"unknown debt stage: {stage}")
    ts = _ts(now or now_msk())
    params = (ts,) * _DEBT_MARK_ARITY.get(stage, 1) + (match_id,)
    with transaction() as conn:
        cursor = conn.execute(sql, params)
        return cursor.rowcount > 0


def _debt_match(cursor, match_id: int) -> dict | None:
    cursor.execute(
        "SELECT id, round_number, division_id, season_id, status, played_at, tournament_type, "
        "player1_id, player2_id, player1_team, player2_team FROM matches WHERE id = ?",
        (match_id,)
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _ensure_debt_row(cursor, m: dict, now: datetime.datetime) -> dict | None:
    """Живая строка долга матча; заводит её, если матч долг, а sync ещё не прошёл.

    None — матч не долг (по той же политике, что `is_match_overdue`).
    """
    debt_row = _load_debt_rows(cursor, [m["id"]]).get(m["id"])
    if debt_row is not None:
        return debt_row
    if m.get("tournament_type") == "cup" or m.get("round_number") == -1:
        return None
    round_row = _match_round_row(cursor, m)
    if not debt_policy.is_debt(m, round_row, None, now):
        return None
    terms = debt_policy.debt_terms(round_row, now)
    if terms is None:
        return None
    _upsert_debt_row(cursor, m, terms, now)
    return _load_debt_rows(cursor, [m["id"]]).get(m["id"])


def _debt_participant_ids(m: dict) -> list[int]:
    ids: list[int] = []
    for side in ("1", "2"):
        pid = m.get(f"player{side}_id")
        if not pid and m.get(f"player{side}_team"):
            owner = find_user_by_team(m.get(f"player{side}_team"), m.get("division_id"))
            pid = owner["telegram_id"] if owner else None
        ids.append(int(pid) if pid else 0)
    return ids


_VERDICTS = {
    "home": (1, 0, "tp_home"),
    "away": (0, 1, "tp_away"),
    "draw": (0, 0, "tech_draw"),
}


def apply_technical_verdict(
    match_id: int,
    verdict: str,
    admin_id: int | None = None,
    now: datetime.datetime | None = None,
) -> dict:
    """ТП / ТН одной транзакцией: счёт, возврат ставок и — только для долга — варны.

    `verdict` — 'home', 'away' или 'draw'. Дисциплина применяется один раз на
    долг: `verdict_applied_at` ставится условным UPDATE, и повторный клик по
    карточке (или вердикт после вердикта) меняет только счёт. Матч, который ещё
    не стал долгом, получает только технический счёт — без варнов.

    ТП: победителю −1 варн за долг, проигравшему +1. ТН: +1 варн обоим.
    Возвращает {is_debt, applied, players, warned, unwarned, kick}, где warned /
    unwarned — списки (user_id, новое число варнов), kick — кого исключить.
    """
    if verdict not in _VERDICTS:
        raise ValueError(f"unknown technical verdict: {verdict}")
    p1_score, p2_score, tech_type = _VERDICTS[verdict]
    now = now or now_msk()
    result = {"is_debt": False, "applied": False, "players": [0, 0],
              "warned": [], "unwarned": [], "kick": []}
    with transaction() as conn:
        cursor = conn.cursor()
        m = _debt_match(cursor, match_id)
        if m is None:
            raise ValueError(f"Match {match_id} not found")
        debt_row = _ensure_debt_row(cursor, m, now)
        p1_id, p2_id = _debt_participant_ids(m)
        result["players"] = [p1_id, p2_id]

        set_technical_result(match_id, p1_score, p2_score, tech_type)

        if debt_row is None:
            return result
        result["is_debt"] = True
        cursor.execute(
            "UPDATE match_debts SET verdict_applied_at = ?, state = 'resolved', resolution = ?, "
            "resolved_at = ?, resolved_by = ? "
            "WHERE match_id = ? AND verdict_applied_at IS NULL AND state != 'cancelled'",
            (_ts(now), tech_type, _ts(now), admin_id, match_id)
        )
        if cursor.rowcount != 1:
            return result
        result["applied"] = True

        rn = m.get("round_number") or 0
        if verdict == "draw":
            losers, winner = [p1_id, p2_id], None
            reason = f"ТН за срыв тура ({rn} тур)"
        else:
            winner, loser = (p1_id, p2_id) if verdict == "home" else (p2_id, p1_id)
            losers = [loser]
            reason = f"ТП за неявку / игнор соперника ({rn} тур)"
        if winner:
            new_cnt, was_unwarned = apply_debt_played_reward(winner, rn)
            if was_unwarned:
                result["unwarned"].append((winner, new_cnt))
        for uid in losers:
            if not uid:
                continue
            new_cnt, exceeded = add_warn(uid, admin_id, reason)
            result["warned"].append((uid, new_cnt))
            if exceeded:
                result["kick"].append(uid)
    return result


def claim_debt_played_reward(match_id: int, now: datetime.datetime | None = None) -> list[tuple[int, int, bool]] | None:
    """Сыгранный долг: −1 варн обоим участникам, один раз на матч.

    Отметка `reward_given_at` и снятие варнов — в одной транзакции, так что
    повторный вызов (черновик + повторный ввод админом) ничего не даёт.
    Возвращает [(user_id, новое число варнов, снят ли варн)] или None, если
    матч не долг, награда уже выдана или по долгу вынесен вердикт.
    """
    now = now or now_msk()
    with transaction() as conn:
        cursor = conn.cursor()
        m = _debt_match(cursor, match_id)
        if m is None:
            return None
        if _ensure_debt_row(cursor, m, now) is None:
            return None
        cursor.execute(
            "UPDATE match_debts SET reward_given_at = ? "
            "WHERE match_id = ? AND reward_given_at IS NULL AND verdict_applied_at IS NULL "
            "AND state != 'cancelled'",
            (_ts(now), match_id)
        )
        if cursor.rowcount != 1:
            return None
        out: list[tuple[int, int, bool]] = []
        seen: set[int] = set()
        for uid in _debt_participant_ids(m):
            if not uid or uid in seen:
                continue
            seen.add(uid)
            new_cnt, was_unwarned = apply_debt_played_reward(uid, m.get("round_number") or 0)
            out.append((uid, new_cnt, was_unwarned))
        return out


# ─── Жизненный цикл тура ──────────────────────────────────────────────────

def validate_round_deadline(deadline_text: str | None, now: datetime.datetime | None = None) -> datetime.datetime:
    """Дедлайн обязателен и должен быть в будущем. Возвращает разобранный момент."""
    text = (deadline_text or "").strip()
    if not text:
        raise RoundDeadlineError(deadline_text, "Дедлайн не указан.")
    try:
        dl = datetime.datetime.strptime(text, "%d.%m.%Y %H:%M")
    except ValueError:
        dl = parse_flexible_datetime(text)
    if dl is None:
        raise RoundDeadlineError(deadline_text, "Неверный формат дедлайна. Используйте ДД.ММ.ГГГГ ЧЧ:ММ.")
    if dl <= (now or now_msk()):
        raise RoundDeadlineError(deadline_text, "Дедлайн уже прошёл — укажите момент в будущем.")
    return dl


def _round_row(cursor, round_number: int, division_id: int, season_id: int) -> dict | None:
    cursor.execute(
        "SELECT * FROM rounds WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ? "
        "ORDER BY season_id IS NULL LIMIT 1",
        (season_id, division_id, round_number)
    )
    row = cursor.fetchone()
    return dict(row) if row else None


def _round_pending_league_matches(cursor, round_number: int, division_id: int, season_id: int) -> list[dict]:
    cursor.execute("""
        SELECT id, round_number, division_id, season_id, player1_id, player2_id, player1_team, player2_team
        FROM matches
        WHERE round_number = ? AND COALESCE(division_id, 1) = ?
          AND (season_id = ? OR season_id IS NULL)
          AND status = 'pending'
          AND (tournament_type IS NULL OR tournament_type = 'league')
        ORDER BY id
    """, (round_number, division_id, season_id))
    return [dict(r) for r in cursor.fetchall()]


def _close_terms(round_row: dict | None, closed_at: datetime.datetime) -> debt_policy.DebtTerms:
    """Срок долга для матчей тура, закрываемого в `closed_at`.

    До дедлайна — регламентные часы плюс остаток до дедлайна; после дедлайна —
    от дедлайна. Старый тур без дедлайна считается от момента закрытия.
    """
    deadline = debt_policy.round_deadline(round_row)
    early = debt_policy.early_close_terms(deadline, closed_at)
    if early is not None:
        return early
    return debt_policy.terms_for(deadline if deadline is not None else closed_at)


def _cancel_round_debts(cursor, round_number: int, division_id: int, season_id: int,
                        now: datetime.datetime, reason: str) -> int:
    """Снять долги тура, по которым ещё не было ни вердикта, ни награды."""
    cursor.execute("""
        UPDATE match_debts SET state = 'cancelled', resolution = ?, resolved_at = ?
        WHERE state IN ('active', 'escalated')
          AND verdict_applied_at IS NULL AND reward_given_at IS NULL
          AND match_id IN (
              SELECT id FROM matches
              WHERE round_number = ? AND COALESCE(division_id, 1) = ?
                AND (season_id = ? OR season_id IS NULL)
                AND status = 'pending'
                AND (tournament_type IS NULL OR tournament_type = 'league')
          )
    """, (reason, _ts(now), round_number, division_id, season_id))
    return cursor.rowcount


def _mark_passed_milestones(cursor, round_number: int, division_id: int,
                            deadline: datetime.datetime, now: datetime.datetime) -> None:
    """Вехи напоминаний, которые к моменту открытия уже позади, считаются отправленными.

    Иначе тур, открытый за 71 час до дедлайна, сразу получал бы «осталось 72 часа».
    """
    hours_left = (deadline - now).total_seconds() / 3600.0
    for h in ROUND_DEADLINE_REMINDER_HOURS:
        if h > hours_left:
            cursor.execute(
                "INSERT OR IGNORE INTO round_reminders (division_id, round_number, reminder_type, sent_at) "
                "VALUES (?, ?, ?, ?)",
                (division_id, round_number, f"{h}h", _ts(now))
            )


def _after_round_opened(cursor, round_number: int, division_id: int, season_id: int,
                        deadline: str | None, now: datetime.datetime) -> None:
    """Статус `open`; долги тура, начатые по прежнему сроку, снимаются."""
    cursor.execute(
        "UPDATE rounds SET status = 'open', closed_at = NULL, closed_by = NULL "
        "WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
        (season_id, division_id, round_number)
    )
    _cancel_round_debts(cursor, round_number, division_id, season_id, now, "deadline_moved")
    dl = parse_flexible_datetime(deadline) if deadline else None
    if dl is not None:
        _mark_passed_milestones(cursor, round_number, division_id, dl, now)


def _mark_round_closed(cursor, round_number: int, division_id: int, season_id: int,
                       closed_by: int | None, now: datetime.datetime) -> int:
    """Статус `closed`, несыгранные матчи — в долг. Возвращает число новых долгов.

    Тур, который ещё не открывали, закрытием не становится: у него нет срока,
    и превращать его расписание в долги нельзя. Повторное закрытие уже
    закрытого тура момент закрытия не сдвигает.
    """
    row = _round_row(cursor, round_number, division_id, season_id)
    if row is None:
        return 0
    stored = row.get("status")
    if stored == debt_policy.ROUND_SCHEDULED or (stored is None and not row.get("deadline")):
        return 0
    closed_at = debt_policy.round_deadline({"deadline": row.get("closed_at")}) if stored == debt_policy.ROUND_CLOSED else None
    if closed_at is None:
        closed_at = now
        cursor.execute(
            "UPDATE rounds SET status = 'closed', closed_at = ?, closed_by = ? "
            "WHERE (season_id = ? OR season_id IS NULL) AND division_id = ? AND round_number = ?",
            (_ts(closed_at), closed_by, season_id, division_id, round_number)
        )
    terms = _close_terms(row, closed_at)
    created = 0
    for m in _round_pending_league_matches(cursor, round_number, division_id, season_id):
        if _upsert_debt_row(cursor, m, terms, now):
            created += 1
    return created


def preview_close_round(round_number: int, division_id: int, season_id: int | None = None,
                        now: datetime.datetime | None = None) -> dict:
    """Что будет, если закрыть тур сейчас: какие матчи уйдут в долг и до какого срока."""
    now = now or now_msk()
    s_id = _resolve_season_id(season_id)
    with transaction() as conn:
        cursor = conn.cursor()
        row = _round_row(cursor, round_number, division_id, s_id)
        matches = _round_pending_league_matches(cursor, round_number, division_id, s_id)
    terms = _close_terms(row, now)
    return {
        "round_number": round_number,
        "division_id": division_id,
        "status": debt_policy.round_phase(row, now),
        "deadline": (row or {}).get("deadline"),
        "matches": matches,
        "early": terms.grace_hours > 0,
        "grace_hours": terms.grace_hours,
        "escalate_at": terms.escalate_at,
    }


def close_round(round_number: int, division_id: int, admin_id: int | None = None,
                season_id: int | None = None) -> dict:
    """Закрыть тур: приём результатов идёт дальше, но несыгранные матчи — уже долги.

    Возвращает срок долга и список новых долгов тура с участниками — для
    объявления и личных сообщений.
    """
    s_id = _resolve_season_id(season_id)
    with _bet_placement_lock, transaction() as conn:
        update_round_status(round_number, is_open=False, division_id=division_id,
                            season_id=s_id, closed_by=admin_id)
        row = _round_row(conn.cursor(), round_number, division_id, s_id)
    closed_at = debt_policy.round_deadline({"deadline": (row or {}).get("closed_at")}) or now_msk()
    terms = _close_terms(row, closed_at)
    debts = [
        m for m in get_detailed_overdue_matches(division_id=division_id, season_id=s_id)
        if m["round_number"] == round_number
    ]
    return {
        "round_number": round_number,
        "division_id": division_id,
        "closed_at": closed_at,
        "early": terms.grace_hours > 0,
        "grace_hours": terms.grace_hours,
        "escalate_at": terms.escalate_at,
        "debts": debts,
    }


def reopen_round(round_number: int, division_id: int, deadline: str,
                 season_id: int | None = None) -> list[int]:
    """Вернуть закрытый тур в игру с новым дедлайном (решение глобального админа).

    Долги тура без вердикта и без награды снимаются; права проверяет хендлер.
    """
    validate_round_deadline(deadline)
    return update_round_status(round_number, is_open=True, deadline=deadline,
                               division_id=division_id, season_id=season_id)


def get_rounds_awaiting_close(now: datetime.datetime | None = None) -> list[dict]:
    """Открытые туры, дедлайн которых прошёл, а админы об этом ещё не уведомлены."""
    now = now or now_msk()
    result = []
    for r in get_open_rounds_with_deadlines():
        dl = parse_flexible_datetime(r.get("deadline"))
        div_id = r.get("division_id") or 1
        if dl is None or dl > now:
            continue
        if has_reminder_been_sent(r["round_number"], "deadline_passed_admin", div_id):
            continue
        s_id = _resolve_season_id(r.get("season_id"))
        with transaction() as conn:
            pending = len(_round_pending_league_matches(conn.cursor(), r["round_number"], div_id, s_id))
        result.append({**r, "division_id": div_id, "pending": pending})
    return result


def _team_owner_resolver(user_rows: list[dict]):
    """Владелец клуба по названию в пределах дивизиона матча (точное имя, затем алиасы)."""
    def get_team_owner(t_name: str | None, match_div_id: int | None = None) -> dict | None:
        if not t_name:
            return None
        t_clean = t_name.strip().lower()
        scoped_users = [u for u in user_rows if match_div_id is None or u.get("division_id") == match_div_id or u.get("division_id") is None]
        for u in scoped_users:
            if (u.get("team_name") or "").strip().lower() == t_clean:
                return u
        for u in scoped_users:
            if teams_match((u.get("team_name") or "").strip(), t_name):
                return u
        return None
    return get_team_owner


def _debt_view(m: dict, r_info: dict | None, terms: debt_policy.DebtTerms, now: datetime.datetime) -> dict:
    """Поля срока долга, которые показывают админка, кабинет и трекер."""
    frozen = debt_policy.frozen_seconds(m, now)
    m["deadline_str"] = (r_info or {}).get("deadline") or "—"
    m["deadline_dt"] = debt_policy.round_deadline(r_info)
    m["became_debt_at"] = terms.became_debt_at
    m["grace_hours"] = terms.grace_hours
    m["escalate_at"] = debt_policy.effective_escalate_at(terms, frozen)
    m["frozen_hours"] = frozen / 3600.0
    m["hours_overdue"] = debt_policy.hours_overdue(terms, frozen, now)
    m["hours_to_escalation"] = debt_policy.hours_to_escalation(terms, frozen, now)
    return m


def get_detailed_overdue_matches(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Несыгранные матчи лиги, которые сейчас долг, с участниками и сроком.

    Долг ли матч, решает `services.debt_policy`: строка `match_debts` (срок,
    записанный при досрочном закрытии тура) или прошедший дедлайн его тура —
    строго тура своего (сезон, дивизион). Тур без дедлайна долгов не порождает.
    `hours_overdue` считается от момента, когда матч стал долгом, без
    замороженного времени; `escalate_at` уже сдвинут на заморозку.
    Участники берутся из `player*_id`, иначе — текущий владелец клуба.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        now = now_msk()
        target_season_id = _resolve_season_id(season_id)

        round_info_map = _load_round_states(_fetch_round_rows(cursor, target_season_id, division_id))

        query = """
            SELECT
                m.id, m.round_number, COALESCE(m.is_extended, 0) AS is_extended,
                COALESCE(m.frozen_seconds, 0) AS frozen_seconds,
                m.frozen_at, m.extended_until, m.status, m.played_at,
                m.player1_id, m.player2_id,
                m.player1_team, m.player2_team, m.division_id, m.season_id
            FROM matches m
            WHERE (m.tournament_type IS NULL OR m.tournament_type = 'league')
              AND m.status = 'pending'
              AND (m.season_id = ? OR m.season_id IS NULL)
        """
        params: list = [target_season_id]
        if division_id is not None:
            query += " AND COALESCE(m.division_id, 1) = ?"
            params.append(division_id)
        query += " ORDER BY m.round_number ASC, m.id ASC"
        cursor.execute(query, tuple(params))
        matches = [dict(row) for row in cursor.fetchall()]
        debt_rows = _load_debt_rows(cursor, [m["id"] for m in matches])

        if division_id is not None:
            cursor.execute("SELECT telegram_id, username, team_name, warn_count, division_id FROM users WHERE team_name IS NOT NULL AND (division_id = ? OR division_id IS NULL)", (division_id,))
        else:
            cursor.execute("SELECT telegram_id, username, team_name, warn_count, division_id FROM users WHERE team_name IS NOT NULL")
        user_rows = [dict(r) for r in cursor.fetchall()]
        user_by_id = {u["telegram_id"]: u for u in user_rows if u.get("telegram_id")}
        get_team_owner = _team_owner_resolver(user_rows)

        overdue_list = []
        for m in matches:
            m_div = m.get("division_id") or 1
            r_info = round_info_map.get((m_div, m["round_number"]))
            debt_row = debt_rows.get(m["id"])
            if not debt_policy.is_debt(m, r_info, debt_row, now):
                continue
            terms = debt_policy.resolve_terms(debt_row, r_info, now)
            if terms is None:
                continue

            u1 = user_by_id.get(m.get("player1_id")) if m.get("player1_id") else None
            if not u1:
                u1 = get_team_owner(m.get("player1_team"), m_div)
            u2 = user_by_id.get(m.get("player2_id")) if m.get("player2_id") else None
            if not u2:
                u2 = get_team_owner(m.get("player2_team"), m_div)

            m["player1_id"] = m.get("player1_id") or (u1.get("telegram_id") if u1 else None)
            m["p1_username"] = u1.get("username") if u1 else None
            m["p1_warns"] = u1.get("warn_count", 0) if u1 else 0
            m["player2_id"] = m.get("player2_id") or (u2.get("telegram_id") if u2 else None)
            m["p2_username"] = u2.get("username") if u2 else None
            m["p2_warns"] = u2.get("warn_count", 0) if u2 else 0
            m["debt"] = debt_row
            overdue_list.append(_debt_view(m, r_info, terms, now))

        return overdue_list


def get_league_overview_rows(season_id: int | None = None) -> dict:
    """Сырые данные для сводки по дивизионам (`/overview`): туры, прогресс, варны.

    Возвращает `{"season_id", "rounds", "match_counts", "warned_users"}`:
      • `rounds` — строки `rounds` сезона (по одной на (дивизион, тур), как в
        `_load_round_states`);
      • `match_counts` — матчи лиги по (дивизион, тур): `total` без отменённых и
        `played` (confirmed / completed / finished);
      • `warned_users` — тренеры с клубом и хотя бы одним варном.
    Долги сюда не входят — их считает `get_detailed_overdue_matches`, чтобы сводка
    и список долгов не расходились.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        target_season_id = _resolve_season_id(season_id)
        rounds = list(_load_round_states(_fetch_round_rows(cursor, target_season_id)).values())

        cursor.execute(
            """
            SELECT COALESCE(division_id, 1) AS division_id, round_number,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status IN ('confirmed', 'completed', 'finished') THEN 1 ELSE 0 END) AS played
            FROM matches
            WHERE (tournament_type IS NULL OR tournament_type = 'league')
              AND (season_id = ? OR season_id IS NULL)
              AND status != 'cancelled'
              AND round_number > 0
            GROUP BY COALESCE(division_id, 1), round_number
            """,
            (target_season_id,)
        )
        match_counts = [dict(r) for r in cursor.fetchall()]

        cursor.execute(
            """
            SELECT telegram_id, username, team_name, COALESCE(division_id, 1) AS division_id,
                   COALESCE(warn_count, 0) AS warn_count
            FROM users
            WHERE COALESCE(warn_count, 0) > 0
              AND team_name IS NOT NULL AND TRIM(team_name) != ''
            ORDER BY warn_count DESC, team_name ASC
            """
        )
        warned_users = [dict(r) for r in cursor.fetchall()]

        return {
            "season_id": target_season_id,
            "rounds": rounds,
            "match_counts": match_counts,
            "warned_users": warned_users,
        }


def _match_round_row(cursor, match: dict) -> dict | None:
    """Строка тура матча — строго его (сезон, дивизион, номер)."""
    s_id = match.get("season_id")
    if s_id is None:
        s_id = _resolve_season_id(None)
    div = match.get("division_id") or 1
    rows = _load_round_states(_fetch_round_rows(cursor, s_id, div))
    return rows.get((div, match["round_number"]))


def is_match_overdue(match_id: int) -> bool:
    """Долг ли матч — то же правило, что в списке долгов (`services.debt_policy`).

    Зовётся и после внесения результата: матч, сыгранный до того, как стал
    долгом, долгом не считается, и награда за него не положена.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, round_number, division_id, season_id, status, played_at, tournament_type "
            "FROM matches WHERE id = ?",
            (match_id,)
        )
        row = cursor.fetchone()
        if not row:
            return False
        m = dict(row)
        debt_row = _load_debt_rows(cursor, [match_id]).get(match_id)
        if m.get("tournament_type") == "cup" or m["round_number"] == -1:
            return debt_row is not None
        return debt_policy.is_debt(m, _match_round_row(cursor, m), debt_row, now_msk())


def division_has_played_matches(division_id: int | None = None, season_id: int | None = None) -> bool:
    """Check if any match in the division and season has already been confirmed or completed."""
    with transaction() as conn:
        cursor = conn.cursor()
        query = "SELECT 1 FROM matches WHERE status IN ('confirmed', 'completed')"
        params = []
        if division_id is not None:
            query += " AND division_id = ?"
            params.append(division_id)
        if season_id is not None:
            query += " AND season_id = ?"
            params.append(season_id)
        else:
            act = get_active_season()
            if act:
                query += " AND (season_id = ? OR season_id IS NULL)"
                params.append(act["id"])
        query += " LIMIT 1"
        cursor.execute(query, tuple(params))
        return cursor.fetchone() is not None


def find_user_by_team(team_name: str | None, division_id: int | None = None) -> dict | None:
    """Find a user record by assigned team name using case-insensitive and smart alias/fuzzy matching, optionally scoped to a division."""
    if not team_name:
        return None
    tn_target = team_name.strip().lower()
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute("SELECT * FROM users WHERE team_name IS NOT NULL AND (division_id = ? OR division_id IS NULL)", (division_id,))
        else:
            cursor.execute("SELECT * FROM users WHERE team_name IS NOT NULL")
        users = [dict(r) for r in cursor.fetchall()]
        
        # 1. Exact case-insensitive match
        for u in users:
            u_team = (u.get("team_name") or "").strip().lower()
            if u_team == tn_target:
                return u

        # 2. Smart teams_match / aliases
        for u in users:
            u_team = (u.get("team_name") or "").strip()
            if teams_match(u_team, team_name):
                return u

    return None


def get_usernames_by_teams(team_names) -> dict[str, str]:
    """Map each given club name (casefolded, trimmed) to its coach's username.

    One read for a whole list — the cup line and bracket need a tag for every
    series. Club names are unique league-wide, so an exact match is enough.
    The comparison is done in Python: SQLite's LOWER() folds ASCII only, and
    the club names are Cyrillic. Clubs without a coach or username are left out.
    """
    wanted = {(n or "").strip().casefold() for n in team_names} - {""}
    if not wanted:
        return {}
    with transaction() as conn:
        rows = conn.execute(
            "SELECT team_name, username FROM users "
            "WHERE team_name IS NOT NULL AND username IS NOT NULL AND TRIM(username) != ''"
        ).fetchall()
    result = {}
    for r in rows:
        key = r["team_name"].strip().casefold()
        if key in wanted:
            result[key] = r["username"].strip()
    return result


def has_user_been_warned_recently(user_id: int, hours: float = 20.0) -> bool:
    """Check if user has received a warn within the last N hours."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT created_at FROM user_warns 
            WHERE user_id = ? AND type = 'WARN_ADD'
            ORDER BY created_at DESC, id DESC LIMIT 1
        """, (user_id,))
        row = cursor.fetchone()
        if not row or not row["created_at"]:
            return False
        warn_dt = parse_flexible_datetime(row["created_at"])
        if not warn_dt:
            return False
        diff_sec = (now_msk() - warn_dt).total_seconds()
        # Negative diff (clock stepped backwards) also counts as "recently warned"
        # so a rollback of the system clock cannot defeat the rate limiter.
        return diff_sec < (hours * 3600.0)



def reset_all_debt_reminders() -> None:
    """Clear all recorded debt stages and reminders."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM debt_reminders")
        cursor.execute(
            "UPDATE match_debts SET state = 'active', last_reminder_at = NULL, soft_warned_at = NULL, escalated_at = NULL, last_escalation_at = NULL, escalation_count = 0, global_escalated_at = NULL "
            "WHERE state IN ('active', 'escalated')"
        )


def admin_reset_all_warns_and_debts() -> int:
    """Reset all user warns to 0, clear debt_reminders, and return count of affected users."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET warn_count = 0 WHERE warn_count > 0")
        affected = cursor.rowcount
        cursor.execute("DELETE FROM debt_reminders")
        cursor.execute(
            "UPDATE match_debts SET state = 'active', last_reminder_at = NULL, soft_warned_at = NULL, escalated_at = NULL, last_escalation_at = NULL, escalation_count = 0, global_escalated_at = NULL "
            "WHERE state IN ('active', 'escalated')"
        )
        cursor.execute("DELETE FROM user_warns")
        return affected


def restore_user_team(telegram_id: int, team_name: str) -> None:
    """Assign/restore team_name for a user and clear warns."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE users SET team_name = ?, warn_count = 0 WHERE telegram_id = ?", (team_name, telegram_id))
        now_str = now_msk_str()
        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, NULL, 'Восстановление клуба и сброс варнов', 'RESTORE', ?)",
            (telegram_id, now_str)
        )



def apply_debt_played_reward(user_id: int, round_number: int) -> tuple[int, bool]:
    """
    Reward player for clearing a debt match by removing 1 warn if warn_count > 0.
    If warn_count == 0, it remains 0.
    Returns (new_warn_count, was_unwarned).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT warn_count FROM users WHERE telegram_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row:
            return 0, False
        current_warns = row["warn_count"] or 0
        if current_warns <= 0:
            return 0, False
        
        new_warns = max(0, current_warns - 1)
        cursor.execute("UPDATE users SET warn_count = ? WHERE telegram_id = ?", (new_warns, user_id))
        now_str = now_msk_str()
        cursor.execute(
            "INSERT INTO user_warns (user_id, admin_id, reason, type, created_at) VALUES (?, NULL, ?, 'DEBT_UNWARN', ?)",
            (user_id, f"Снятие варна за закрытие долга ({round_number} тур)", now_str)
        )
        return new_warns, True


def count_user_remaining_debts(user_id: int) -> int:
    """Сколько долгов осталось у игрока — в его дивизионе, по той же политике."""
    with transaction() as conn:
        row = conn.execute("SELECT division_id FROM users WHERE telegram_id = ?", (user_id,)).fetchone()
    div_id = row["division_id"] if row and row["division_id"] is not None else None
    overdue_matches = get_detailed_overdue_matches(division_id=div_id)
    return sum(
        1 for m in overdue_matches
        if m.get("player1_id") == user_id or m.get("player2_id") == user_id
    )


# ═════════════════════════════════════════════════════════════════════════════
# 🎰 LOGOVO.BET — VIRTUAL SPORTS PREDICTION & BETTING REPOSITORY
# ═════════════════════════════════════════════════════════════════════════════

def get_active_round_number() -> int:
    """Return the lowest currently open round number or the latest created round."""
    try:
        open_rounds = get_open_rounds_with_deadlines()
        if open_rounds:
            return min(r["round_number"] for r in open_rounds)
        all_rounds = get_all_rounds()
        if all_rounds:
            return max(r["round_number"] for r in all_rounds)
    except Exception:
        pass
    return 1


def get_user_open_exposure(user_id: int, cursor=None) -> int:
    """
    Sum of potential_win over the user's pending bets that count toward the
    open-exposure limit. Bets placed before the 10k payout cap
    (legacy_limits = 1) keep the old rules and are left out.
    """
    sql = """
        SELECT COALESCE(SUM(potential_win), 0) AS open_exposure
        FROM user_bets
        WHERE user_id = ? AND status = 'pending' AND COALESCE(legacy_limits, 0) = 0
    """
    if cursor is not None:
        cursor.execute(sql, (user_id,))
        return int(cursor.fetchone()["open_exposure"])
    with transaction() as conn:
        row = conn.execute(sql, (user_id,)).fetchone()
        return int(row["open_exposure"])


def get_user_open_bets_count(user_id: int, cursor=None) -> int:
    """
    Сколько купонов игрока сейчас открыто (ждут расчёта).

    Считаются именно купоны, а не исходы: экспресс из пяти матчей — одна строка
    в `user_bets` и один занятый слот.

    В отличие от `get_user_open_exposure`, флаг `legacy_limits` здесь не
    учитывается. Тот флаг выводит купоны, принятые до потолка выплат, из лимита
    *суммы* — их считали по старым правилам, и задним числом занимать ими
    ответственность было бы нечестно. Лимит *количества* — про другое: он
    ограничивает, сколько пари человек ведёт одновременно, и открытый купон
    занимает слот независимо от того, по каким правилам его приняли.
    """
    sql = """
        SELECT COUNT(*) AS open_bets
        FROM user_bets
        WHERE user_id = ? AND status = 'pending'
    """
    if cursor is not None:
        cursor.execute(sql, (user_id,))
        return int(cursor.fetchone()["open_bets"])
    with transaction() as conn:
        row = conn.execute(sql, (user_id,)).fetchone()
        return int(row["open_bets"])


def get_or_create_wallet(user_id: int) -> dict:
    """Get user's betting wallet or initialize a new one with the starting balance.

    The starting balance is config.INITIAL_WALLET_BALANCE unless a global admin
    changed it in the panel (see get_initial_wallet_balance).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        start_balance = get_initial_wallet_balance()
        cursor.execute(
            """
            INSERT INTO user_wallets (user_id, balance, total_wagered, total_won, bets_count, bets_won, updated_at)
            VALUES (?, ?, 0, 0, 0, 0, datetime('now', '+3 hours'))
            """,
            (user_id, start_balance)
        )
        cursor.execute(
            "INSERT INTO coin_transactions (user_id, amount, transaction_type, balance_after, created_at)"
            " VALUES (?, ?, 'welcome_bonus', ?, datetime('now', '+3 hours'))",
            (user_id, start_balance, start_balance)
        )
        cursor.execute("SELECT * FROM user_wallets WHERE user_id = ?", (user_id,))
        new_row = cursor.fetchone()
        return dict(new_row) if new_row else {"user_id": user_id, "balance": start_balance}


def get_wallet_balance(user_id: int) -> int:
    """Get the current coin balance of a user."""
    wallet = get_or_create_wallet(user_id)
    return wallet.get("balance", 0)


def add_coins(user_id: int, amount: int, tx_type: str = "deposit", ref_id: int | None = None) -> int:
    """Safely credit coins to user's wallet with transaction log."""
    if amount <= 0:
        return get_wallet_balance(user_id)

    with transaction() as conn:
        cursor = conn.cursor()
        get_or_create_wallet(user_id)
        cursor.execute(
            "UPDATE user_wallets SET balance = balance + ?, updated_at = datetime('now', '+3 hours') WHERE user_id = ?",
            (amount, user_id)
        )
        cursor.execute(
            "INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, balance_after, created_at)"
            " VALUES (?, ?, ?, ?, (SELECT balance FROM user_wallets WHERE user_id = ?), datetime('now', '+3 hours'))",
            (user_id, amount, tx_type, ref_id, user_id)
        )
        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return row["balance"] if row else 0


def deduct_coins(user_id: int, amount: int, tx_type: str = "bet_placed", ref_id: int | None = None) -> bool:
    """Deduct coins from wallet if balance is sufficient."""
    if amount <= 0:
        return False

    with transaction() as conn:
        cursor = conn.cursor()
        get_or_create_wallet(user_id)
        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if not row or row["balance"] < amount:
            return False

        cursor.execute(
            """
            UPDATE user_wallets 
            SET balance = balance - ?, total_wagered = total_wagered + ?, updated_at = datetime('now', '+3 hours') 
            WHERE user_id = ?
            """,
            (amount, amount, user_id)
        )
        cursor.execute(
            "INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, balance_after, created_at)"
            " VALUES (?, ?, ?, ?, (SELECT balance FROM user_wallets WHERE user_id = ?), datetime('now', '+3 hours'))",
            (user_id, -amount, tx_type, ref_id, user_id)
        )
        return True


def get_top_bettors(limit: int = 10) -> list[dict]:
    """Leaderboard of top bettors by net coin balance and win rate."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT w.user_id, w.balance, w.total_won, w.bets_count, w.bets_won, u.username, u.team_name
            FROM user_wallets w
            LEFT JOIN users u ON w.user_id = u.telegram_id
            ORDER BY w.balance DESC, w.total_won DESC
            LIMIT ?
            """,
            (limit,)
        )
        return [dict(r) for r in cursor.fetchall()]


def save_bet_market(
    match_id: int,
    tour: int,
    team1_name: str,
    team2_name: str,
    odd_p1: float,
    odd_x: float,
    odd_p2: float,
    odd_tb25: float = 1.80,
    odd_tm25: float = 1.95,
    odd_btts_yes: float = 1.70,
    odd_btts_no: float = 2.05
) -> int:
    """Save or update betting odds for a given match."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bet_markets (
                match_id, tour, team1_name, team2_name,
                odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, odd_btts_yes, odd_btts_no, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, datetime('now', '+3 hours'))
            ON CONFLICT(match_id) DO UPDATE SET
                tour = excluded.tour,
                team1_name = excluded.team1_name,
                team2_name = excluded.team2_name,
                odd_p1 = excluded.odd_p1,
                odd_x = excluded.odd_x,
                odd_p2 = excluded.odd_p2,
                odd_tb25 = excluded.odd_tb25,
                odd_tm25 = excluded.odd_tm25,
                odd_btts_yes = excluded.odd_btts_yes,
                odd_btts_no = excluded.odd_btts_no,
                is_active = 1
            """,
            (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, odd_tb25, odd_tm25, odd_btts_yes, odd_btts_no)
        )
        return cursor.lastrowid or match_id


def _parse_round_deadline(dl_str: str | None) -> datetime.datetime | None:
    """Safely parse deadline string from various common formats."""
    if not dl_str:
        return None
    dl_clean = str(dl_str).strip()
    formats = [
        "%d.%m.%Y %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            return datetime.datetime.strptime(dl_clean, fmt)
        except Exception:
            continue
    try:
        return datetime.datetime.fromisoformat(dl_clean)
    except Exception:
        return None


def get_open_betting_tours(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """
    Retrieve all currently open tours that have unplayed matches
    and where the round deadline has not expired.
    Тур попадает в линию ровно по тому же правилу, что применяет
    `evaluate_round_betting_gate`: линия открыта (`bets_open = 1`), а тур
    ещё не открыт для игры (`is_open = 0`). Список не может показать тур,
    ставку на который сервер всё равно отклонит.

    Scope: матчи джойнятся к своему туру по полному ключу
    `season_id + division_id + round_number`, поэтому тур одного дивизиона
    никогда не «подхватывает» матчи другого. Сезон не задан — берётся активный,
    а не «все сразу»: линия завершённого сезона в списке появиться не должна.
    Дивизион не задан — возвращаются туры всех дивизионов активного сезона,
    каждый со своим `division_id` (существующий контракт Telegram-меню ставок).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is None:
            act = get_active_season()
            season_id = act["id"] if act else 1
        query = """
            SELECT
                r.round_number, r.deadline, r.division_id, r.season_id,
                r.is_open, COALESCE(r.bets_open, 0) AS bets_open,
                COUNT(m.id) as total_matches,
                SUM(CASE WHEN m.status NOT IN ('confirmed', 'completed') THEN 1 ELSE 0 END) as unplayed_matches
            FROM rounds r
            JOIN matches m
              ON m.round_number = r.round_number
             AND COALESCE(m.division_id, 1) = r.division_id
             AND COALESCE(m.season_id, 1) = r.season_id
            WHERE r.is_open = 0 AND COALESCE(r.bets_open, 0) = 1
              AND r.season_id = ?
        """
        params = [season_id]
        if division_id is not None:
            query += " AND r.division_id = ?"
            params.append(division_id)

        query += """
            GROUP BY r.round_number, r.deadline, r.division_id, r.season_id, r.is_open, r.bets_open
            HAVING unplayed_matches > 0
            ORDER BY r.round_number ASC
        """
        cursor.execute(query, params)
        rows = cursor.fetchall()
        now = now_msk()
        open_tours = []
        for row in rows:
            r_num = row["round_number"]
            dl_str = row["deadline"]
            dl_dt = _parse_round_deadline(dl_str)
            if dl_dt and now > dl_dt:
                # Deadline passed: закрываем линию тура в обеих схемах, а не только в legacy.
                close_round_betting_line(
                    cursor, r_num,
                    division_id=row["division_id"],
                    season_id=row["season_id"]
                )
                continue

            open_tours.append({
                "round_number": r_num,
                "deadline": dl_str,
                "division_id": row["division_id"],
                "season_id": row["season_id"],
                "total_matches": row["total_matches"],
                "unplayed_matches": row["unplayed_matches"],
                # Ранняя линия: тур ещё не открыт для игры, но прогнозы уже принимаются.
                "is_open": bool(row["is_open"]),
                "is_early": (not row["is_open"]) and bool(row["bets_open"])
            })
        return open_tours


def get_active_bet_markets(
    tour: int | None = None,
    division_id: int | None = None,
    season_id: int | None = None
) -> list[dict]:
    """Retrieve open betting markets for unplayed matches in open or pre-opened rounds.

    Scope: legacy `bet_markets` не хранит division/season, поэтому scope берётся
    у матча, а строка тура джойнится по полному ключу
    `season_id + division_id + round_number`. Открытая линия Дивизиона 2
    не может сделать «активным» рынок Дивизиона 1 и наоборот.
    Сезон не задан — берётся активный, а не «любой».
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is None:
            act = get_active_season()
            season_id = act["id"] if act else 1
        query = """
            SELECT bm.*, m.status as match_status, m.round_number, m.division_id, r.deadline, r.is_open,
                   COALESCE(r.bets_open, 0) AS bets_open,
                   COALESCE(u1_id.username, u1_team.username) AS player1_username,
                   COALESCE(u2_id.username, u2_team.username) AS player2_username
            FROM bet_markets bm
            JOIN matches m ON bm.match_id = m.id
            JOIN rounds r
              ON r.round_number = m.round_number
             AND r.division_id = COALESCE(m.division_id, 1)
             AND r.season_id = COALESCE(m.season_id, 1)
            LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
            LEFT JOIN users u1_team ON LOWER(m.player1_team) = LOWER(u1_team.team_name)
            LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
            LEFT JOIN users u2_team ON LOWER(m.player2_team) = LOWER(u2_team.team_name)
            WHERE bm.is_active = 1 AND m.status NOT IN ('confirmed', 'completed')
              AND (r.is_open = 1 OR COALESCE(r.bets_open, 0) = 1)
              AND COALESCE(m.season_id, 1) = ?
        """
        params = [season_id]
        if tour is not None:
            query += " AND bm.tour = ?"
            params.append(tour)
        if division_id is not None:
            query += " AND COALESCE(m.division_id, 1) = ?"
            params.append(division_id)
        query += " ORDER BY bm.tour ASC, bm.id ASC"
        cursor.execute(query, params)
        rows = cursor.fetchall()
        now = now_msk()
        valid_markets = []
        for r in rows:
            dl_dt = _parse_round_deadline(r["deadline"])
            if dl_dt and now > dl_dt:
                cursor.execute("UPDATE bet_markets SET is_active = 0 WHERE match_id = ?", (r["match_id"],))
                continue
            item = dict(r)
            if not item.get("player1_username") and item.get("team1_name"):
                u = find_user_by_team(item["team1_name"])
                if u:
                    item["player1_username"] = u.get("username")
            if not item.get("player2_username") and item.get("team2_name"):
                u = find_user_by_team(item["team2_name"])
                if u:
                    item["player2_username"] = u.get("username")
            valid_markets.append(item)
        return valid_markets


def get_bet_market_by_match_id(match_id: int) -> dict | None:
    """Fetch market odds for a specific match ID if ITS OWN round accepts bets.

    Scope: строка тура берётся строго по `season_id + division_id + round_number`
    самого матча. Джойн только по номеру тура позволял открытому туру чужого
    дивизиона или прошлого сезона сделать матч «доступным для ставки».
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bm.*, r.is_open, COALESCE(r.bets_open, 0) AS bets_open, r.deadline,
                   COALESCE(u1_id.username, u1_team.username) AS player1_username,
                   COALESCE(u2_id.username, u2_team.username) AS player2_username
            FROM bet_markets bm
            JOIN matches m ON bm.match_id = m.id
            JOIN rounds r
              ON r.round_number = m.round_number
             AND r.division_id = COALESCE(m.division_id, 1)
             AND r.season_id = COALESCE(m.season_id, 1)
            LEFT JOIN users u1_id ON m.player1_id = u1_id.telegram_id
            LEFT JOIN users u1_team ON LOWER(m.player1_team) = LOWER(u1_team.team_name)
            LEFT JOIN users u2_id ON m.player2_id = u2_id.telegram_id
            LEFT JOIN users u2_team ON LOWER(m.player2_team) = LOWER(u2_team.team_name)
            WHERE bm.match_id = ?
              AND (r.is_open = 1 OR COALESCE(r.bets_open, 0) = 1)
              AND m.status NOT IN ('confirmed', 'completed')
        """, (match_id,))
        row = cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        if not item.get("player1_username") and item.get("team1_name"):
            u = find_user_by_team(item["team1_name"])
            if u:
                item["player1_username"] = u.get("username")
        if not item.get("player2_username") and item.get("team2_name"):
            u = find_user_by_team(item["team2_name"])
            if u:
                item["player2_username"] = u.get("username")
        return item


# Phase 5: Bet limits (server-side, cannot be bypassed by client)
_MAX_BET: int = 50_000
_MAX_PAYOUT: int = 10_000
# Длина экспресса: от 2 до 15 событий. Один исход — это ординар. Потолок —
# значение по умолчанию: главный админ меняет его в панели (get_max_express_events).
MIN_EXPRESS_EVENTS: int = 2
MAX_EXPRESS_EVENTS: int = 15
# Надбавка на экспресс: итоговый коэффициент умножается на (1 − p/100) за каждое
# событие после первого. Ошибка линии в экспрессе перемножается, и длинные
# купоны были главной утечкой монет. Процент меняется в панели
# (get_express_margin_pct); купон запоминает его в user_bets.express_margin_pct,
# поэтому расчёт всегда идёт по правилу, действовавшему при приёме.
EXPRESS_MARGIN_PCT: int = 3
MAX_EXPRESS_MARGIN_PCT: int = 20
_bet_placement_lock = threading.RLock()


def express_odd(leg_odds, margin_pct) -> float:
    """Итоговый коэффициент купона: произведение ног с надбавкой на экспресс.

    Надбавка берётся за каждую ногу после первой, поэтому ординар и экспресс, в
    котором выиграла одна нога (остальные возвращены), идут по чистому кэфу.
    `margin_pct` = None или 0 — купон без надбавки (принят до её введения).
    Не опускается ниже 1.01: выигравший купон не выплачивает меньше ставки.
    """
    odds = [max(1.01, float(o)) for o in leg_odds]
    raw = 1.0
    for o in odds:
        raw *= o
    pct = max(0, min(MAX_EXPRESS_MARGIN_PCT, int(margin_pct or 0)))
    if len(odds) < 2 or pct == 0:
        return round(raw, 2)
    factor = (1.0 - pct / 100.0) ** (len(odds) - 1)
    return round(max(1.01, raw * factor), 2)


def selection_identity(match_id, outcome, selection_id=None) -> tuple:
    """Ключ исхода для сравнения купонов: исход линии, а без него — матч и исход."""
    if selection_id:
        return ("s", int(selection_id))
    key = str(outcome or "").strip().lower()
    return ("o", int(match_id), OUTCOME_KEY_ALIASES.get(key, key))


def get_identical_open_payout(cursor, user_id: int, identities) -> int:
    """Возможная выплата по открытым купонам игрока с тем же набором исходов.

    Потолок выплаты считается на набор, а не на купон: второй такой же экспресс
    иначе удваивал выигрыш (купоны #692/#693). Купоны до потолка
    (legacy_limits = 1) идут по старым правилам и не учитываются.
    """
    wanted = frozenset(identities)
    if not wanted:
        return 0
    cursor.execute("""
        SELECT ub.id, ub.potential_win, bi.match_id, bi.outcome_type, bi.selection_id
        FROM user_bets ub
        JOIN bet_items bi ON bi.bet_id = ub.id
        WHERE ub.user_id = ? AND ub.status = 'pending' AND COALESCE(ub.legacy_limits, 0) = 0
    """, (user_id,))
    bets: dict[int, dict] = {}
    for r in cursor.fetchall():
        b = bets.setdefault(r["id"], {"payout": int(r["potential_win"] or 0), "keys": set()})
        b["keys"].add(selection_identity(r["match_id"], r["outcome_type"], r["selection_id"]))
    return sum(b["payout"] for b in bets.values() if b["keys"] == wanted)

# Canonical Outcome Aliases & Cross-Schema Mapping
OUTCOME_KEY_ALIASES: dict[str, str] = {
    # Totals 2.5
    "tb25": "over_2.5",
    "over_2.5": "over_2.5",
    "tm25": "under_2.5",
    "under_2.5": "under_2.5",
    # BTTS
    "btts_yes": "btts_yes",
    "yes": "btts_yes",
    "btts_no": "btts_no",
    "no": "btts_no",
    # 1X2
    "p1": "p1",
    "1": "p1",
    "x": "x",
    "draw": "x",
    "p2": "p2",
    "2": "p2",
}

LEGACY_BET_MARKET_COLUMNS: dict[str, str] = {
    "p1": "odd_p1",
    "1": "odd_p1",
    "x": "odd_x",
    "draw": "odd_x",
    "p2": "odd_p2",
    "2": "odd_p2",
    "tb25": "odd_tb25",
    "over_2.5": "odd_tb25",
    "tm25": "odd_tm25",
    "under_2.5": "odd_tm25",
    "btts_yes": "odd_btts_yes",
    "yes": "odd_btts_yes",
    "btts_no": "odd_btts_no",
    "no": "odd_btts_no",
}


def normalize_outcome_key(out_type: str) -> str:
    """Normalize outcome alias to canonical key (e.g. 'tb25' -> 'over_2.5')."""
    if not out_type:
        return ""
    clean = str(out_type).strip().lower()
    return OUTCOME_KEY_ALIASES.get(clean, clean)


def get_possible_outcome_keys(out_type: str) -> list[str]:
    """Return all equivalent keys for SQL lookup (e.g. ['over_2.5', 'tb25'])."""
    clean = str(out_type).strip().lower()
    canonical = OUTCOME_KEY_ALIASES.get(clean, clean)
    keys = [clean, canonical]
    if canonical == "over_2.5":
        keys.extend(["tb25", "over_2.5"])
    elif canonical == "under_2.5":
        keys.extend(["tm25", "under_2.5"])
    elif canonical == "btts_yes":
        keys.extend(["yes", "btts_yes"])
    elif canonical == "btts_no":
        keys.extend(["no", "btts_no"])
    elif canonical == "p1":
        keys.extend(["1", "p1"])
    elif canonical == "p2":
        keys.extend(["2", "p2"])
    elif canonical == "x":
        keys.extend(["draw", "x"])
    seen = set()
    result = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            result.append(k)
    return result


def place_user_bet(
    user_id: int,
    amount: int,
    selections: list[dict],
    idempotency_key: str | None = None
) -> tuple[bool, int | str | dict]:
    """
    Validate, deduct coins, and store user prediction coupon atomically.
    Supports single & express parlays, relational selections, and idempotency protection.
    Phase 5: Adds MAX_BET/MAX_PAYOUT limits, ODDS_CHANGED detection, IDEMPOTENCY_KEY_REUSED.
    """
    from config import is_global_lockdown_enabled
    if is_global_lockdown_enabled():
        from handlers.base import is_global_admin
        if not is_global_admin(user_id):
            return False, {"error": "LOGOVO_LOCKDOWN", "message": "Logovo.bet временно закрыт для пользователей"}

    import datetime
    import hashlib as _hashlib
    import json as _json

    try:
        if isinstance(amount, float) and not amount.is_integer():
            return False, "Сумма ставки должна быть целым числом."
        amount = int(amount)
    except (ValueError, TypeError):
        return False, "Некорректная сумма ставки."

    if amount < 10:
        return False, "Минимальная сумма прогноза — 10 🪙."

    if amount > _MAX_BET:
        return False, {"error": "MAX_BET_EXCEEDED", "max_bet": _MAX_BET,
                       "message": f"Максимальная сумма ставки — {_MAX_BET:,} 🪙."}

    if not selections or not isinstance(selections, list):
        return False, "Купон пуст."

    # Validate each selection structure and strictly normalize match_id to positive int
    normalized_selections = []
    pre_seen_matches = set()
    for s in selections:
        if not isinstance(s, dict):
            return False, "Некорректная структура исхода в купоне."
        raw_mid = s.get("match_id")
        try:
            if isinstance(raw_mid, float) and not raw_mid.is_integer():
                return False, f"Некорректный ID матча: {raw_mid}"
            if isinstance(raw_mid, str):
                raw_mid_clean = raw_mid.strip()
                if "." in raw_mid_clean:
                    return False, f"Некорректный ID матча: {raw_mid}"
                m_id = int(raw_mid_clean)
            elif isinstance(raw_mid, int):
                m_id = raw_mid
            else:
                return False, f"Некорректный ID матча: {raw_mid}"
            if m_id <= 0:
                return False, f"Некорректный ID матча: {raw_mid}"
        except (ValueError, TypeError):
            return False, f"Некорректный ID матча: {raw_mid}"

        out_type = s.get("outcome") or s.get("selection_key")
        if not out_type:
            return False, "Некорректная структура исхода в купоне."

        # SGP check: cannot add multiple outcomes from the same match to an express coupon
        if len(selections) > 1 and m_id in pre_seen_matches:
            return False, f"Нельзя добавлять несколько исходов из одного матча #{m_id} в стандартный экспресс."
        pre_seen_matches.add(m_id)

        s_copy = dict(s)
        s_copy["match_id"] = m_id
        s_copy["outcome"] = str(out_type)
        normalized_selections.append(s_copy)

    selections = normalized_selections

    # Один исход — ординар; от двух до потолка (по умолчанию 15) — экспресс.
    # Лишнее событие не принимается ни из Telegram, ни из Mini App, ни из REST API.
    max_express_events = get_max_express_events()
    if len(selections) > max_express_events:
        return False, {
            "error": "MAX_EXPRESS_EVENTS_EXCEEDED",
            "max_events": max_express_events,
            "message": f"⚠️ В экспрессе может быть максимум {max_express_events} событий!"
        }

    # Compute idempotency payload hash (Phase 5: strictly typed tuple (int, str) to avoid TypeError in sorted)
    _payload_for_hash = _json.dumps(
        {"amount": amount, "sel": sorted(
            [(int(s["match_id"]), str(s.get("outcome") or s.get("selection_key") or "")) for s in selections]
        )},
        sort_keys=True, separators=(',', ':')
    )
    _payload_hash = _hashlib.sha256(_payload_for_hash.encode()).hexdigest()

    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()

        # Idempotency 2.0: key + payload hash check (Phase 5)
        if idempotency_key:
            cursor.execute(
                "SELECT id, idempotency_payload_hash FROM user_bets WHERE user_id = ? AND idempotency_key = ?",
                (user_id, idempotency_key)
            )
            existing = cursor.fetchone()
            if existing:
                existing_hash = existing["idempotency_payload_hash"]
                if existing_hash and existing_hash != _payload_hash:
                    return False, {"error": "IDEMPOTENCY_KEY_REUSED",
                                   "message": "Ключ идемпотентности уже использован для другой ставки."}
                return True, existing["id"]

        # Запрет ставок игроку и экстренная остановка приёма из админ-панели.
        # Проверка стоит в той же транзакции, что и списание, и работает
        # FAIL-CLOSED: если её не удалось выполнить, купон не принимается.
        try:
            block = _betting_block_reason(cursor, user_id, [s["match_id"] for s in selections])
        except Exception:
            logger.exception(f"Betting block check failed for user_id={user_id}; bet rejected")
            block = {"error": "BETTING_UNAVAILABLE",
                     "message": "Приём ставок временно недоступен. Попробуйте позже."}
        if block is not None:
            return False, block

        # 322-защита (дублирующая проверка). RiskEngine отклоняет такие купоны
        # раньше, но эта проверка выполняется в той же транзакции, что и списание
        # монет: она закрывает и гонку (матч мог получить участника между
        # проверкой риск-движка и записью купона), и обход RiskEngine при прямом
        # вызове place_user_bet из API/скрипта.
        self_match_id = find_self_participation_match(cursor, user_id, selections)
        if self_match_id is not None:
            logger.warning(
                f"SELF_BET_PROHIBITED: user_id={user_id} попытался поставить на свой матч "
                f"#{self_match_id} (amount={amount}, selections={len(selections)})"
            )
            return False, {"error": SELF_BET_ERROR_CODE, "message": SELF_BET_ERROR_MESSAGE}

        # Phase 9: Risk Engine Evaluation.
        #
        # Риск-контроль работает FAIL-CLOSED. Внутренняя поломка RiskEngine
        # (ошибка БД, отсутствующая таблица лимитов, сбой exposure-запроса, любой
        # неожиданный exception) — это НЕ разрешение. Такая ставка отклоняется:
        # монеты не списываются, купон не создаётся, транзакция не считается
        # успешной, idempotency-запись не появляется. Пользователю уходит обычный
        # безопасный отказ без деталей исключения.
        div_id = None
        risk_ctx_round = None
        risk_ctx_season = None
        try:
            from services.risk_engine import RiskEngine
            first_m_id = selections[0].get("match_id") if selections else None
            if first_m_id:
                cursor.execute(
                    "SELECT division_id, round_number, season_id FROM matches WHERE id = ?",
                    (first_m_id,)
                )
                m_r = cursor.fetchone()
                if m_r:
                    keys = m_r.keys()
                    # Legacy-строка матча с division_id IS NULL — это дивизион 1
                    # (соглашение из evaluate_round_betting_gate и из per-selection
                    # проверки ниже). Без нормализации RiskEngine получил бы None,
                    # взял бы системные лимиты вместо лимитов дивизиона и пропустил
                    # ветку division_exposure_limit.
                    div_id = m_r["division_id"] if "division_id" in keys and m_r["division_id"] is not None else 1
                    risk_ctx_round = m_r["round_number"] if "round_number" in keys else None
                    risk_ctx_season = m_r["season_id"] if "season_id" in keys else None

            risk_decision = RiskEngine.evaluate_bet(
                user_id=user_id,
                amount=amount,
                selections=selections,
                division_id=div_id
            )
            if risk_decision is None or not hasattr(risk_decision, "allowed"):
                # Некорректные внутренние данные риск-движка — тоже внутренняя ошибка.
                raise RuntimeError("RiskEngine returned a malformed decision object")
        except Exception:
            # Только внутренняя ошибка риск-движка. Бизнес-отказы RiskEngine
            # возвращаются ниже как обычные REJECT и сюда не попадают.
            _risk_match_ids = [s.get("match_id") for s in selections if isinstance(s, dict)]
            logger.exception(
                "RISK_CHECK_UNAVAILABLE: RiskEngine failed during place_user_bet — "
                "bet rejected (fail-closed). "
                f"user_id={user_id} amount={amount} match_ids={_risk_match_ids} "
                f"round_number={risk_ctx_round} division_id={div_id} season_id={risk_ctx_season}"
            )
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return False, {
                "error": "RISK_CHECK_UNAVAILABLE",
                "message": "Не удалось проверить прогноз. Ставка не принята, монеты не списаны. Попробуйте позже."
            }

        if not risk_decision.allowed:
            if risk_decision.reason == "MIN_STAKE":
                return False, "Минимальная сумма прогноза — 10 🪙."
            if risk_decision.reason == "MAX_STAKE":
                # Отказал применённый лимит (division/user override из
                # BettingLimitsService), а не глобальный потолок — игроку
                # показываем именно его.
                max_bet = int((risk_decision.details or {}).get("max_bet") or _MAX_BET)
                return False, {"error": "MAX_BET_EXCEEDED", "max_bet": max_bet,
                               "message": f"Максимальная сумма ставки — {max_bet:,} 🪙."}
            if risk_decision.reason == "MAX_PAYOUT":
                details = risk_decision.details or {}
                err = {"error": "MAX_PAYOUT_EXCEEDED",
                       "max_payout": details.get("max_payout", _MAX_PAYOUT),
                       "message": risk_decision.message
                       or f"Потенциальный выигрыш превышает максимум {_MAX_PAYOUT:,} 🪙."}
                if risk_decision.max_allowed_stake is not None:
                    err["max_allowed_stake"] = risk_decision.max_allowed_stake
                return False, err
            if risk_decision.reason == "INSUFFICIENT_BALANCE":
                wallet = get_or_create_wallet(user_id)
                return False, f"Недостаточно монет на балансе (Баланс: {wallet['balance']} 🪙)."
            if risk_decision.reason in ("MARKET_SUSPENDED", "INVALID_MARKET"):
                return False, risk_decision.message or "Рынок на данный исход временно приостановлен или закрыт."
            if risk_decision.reason == "ODDS_CHANGED":
                details = risk_decision.details or {}
                return False, {
                    "error": "ODDS_CHANGED",
                    "match_id": details.get("match_id"),
                    "outcome": details.get("outcome"),
                    "old_odd": details.get("old_odd"),
                    "new_odd": details.get("new_odd"),
                    "message": risk_decision.message or f"Коэффициент изменился: {details.get('old_odd')} → {details.get('new_odd')}"
                }

            if risk_decision.reason == "MARKET_NOT_OFFERED":
                # Кубковый исход, которого в росписи нет вовсе (`x`, `12`, ИТБ на
                # заголовке серии). Матч и исход отдаются структурированно: Mini App
                # подсвечивает конкретную ногу купона, а не показывает общий отказ.
                details = risk_decision.details or {}
                return False, {
                    "error": "MARKET_NOT_OFFERED",
                    "match_id": details.get("match_id"),
                    "outcome": details.get("outcome"),
                    "message": risk_decision.message
                    or "Исход в линии общего кубка не разыгрывается."
                }
            if risk_decision.reason in ("BET_TYPE_BANNED", "EXPRESS_BANNED"):
                # Запрет вида ставки из панели: называем матч и группу, чтобы
                # Mini App мог показать, какая нога купона под запретом.
                details = risk_decision.details or {}
                return False, {
                    "error": risk_decision.reason,
                    "match_id": details.get("match_id"),
                    "outcome": details.get("outcome"),
                    "group": details.get("group"),
                    "message": risk_decision.message,
                }
            if risk_decision.reason == "OPEN_BETS_LIMIT":
                # Отдаём и сам потолок, и фактическое число открытых купонов:
                # Mini App показывает счётчик слотов и поправит его по ответу,
                # не дожидаясь следующего bootstrap.
                details = risk_decision.details or {}
                return False, {
                    "error": "OPEN_BETS_LIMIT",
                    "max_open_bets": details.get("max_open_bets"),
                    "open_bets": details.get("open_bets"),
                    "message": risk_decision.message
                    or "Слишком много открытых купонов. Дождитесь расчёта."
                }

            err_dict = {
                "error": risk_decision.reason,
                "message": risk_decision.message,
            }
            if risk_decision.max_allowed_stake is not None:
                err_dict["max_allowed_stake"] = risk_decision.max_allowed_stake
            return False, err_dict

        wallet = get_or_create_wallet(user_id)
        if wallet["balance"] < amount:
            return False, f"Недостаточно монет на балансе (Баланс: {wallet['balance']} 🪙)."

        # Validate selections
        validated_items = []
        seen_matches = set()
        seen_cup_series = set()

        for s in selections:
            m_id = s.get("match_id")
            out_type = s.get("outcome") or s.get("selection_key")
            mkt_id = s.get("market_id")
            sel_id = s.get("selection_id")

            if not m_id or not out_type:
                return False, "Некорректная структура исхода в купоне."

            if len(selections) > 1 and m_id in seen_matches:
                return False, f"Нельзя добавлять несколько исходов из одного матча #{m_id} в стандартный экспресс."
            seen_matches.add(m_id)

            # Check match status
            cursor.execute("SELECT * FROM matches WHERE id = ?", (m_id,))
            match_row = cursor.fetchone()
            if not match_row:
                return False, f"Матч #{m_id} не найден."
            if match_row["status"] not in ("scheduled", "pending", "live", "open"):
                return False, f"Матч #{m_id} уже сыгран или завершен (статус: {match_row['status']})."

            # Единое серверное правило приёма ставок: is_open = 0 AND bets_open = 1.
            # То же самое правило применяет RiskEngine — Telegram, Mini App и REST API
            # проверяются одним инвариантом. Разрешающего fallback здесь нет:
            # если строки тура (или этапа) нет, ставка отклоняется.
            allowed, _reason, gate_message = evaluate_betting_gate(cursor, match_row, match_id=m_id)
            if not allowed:
                return False, gate_message

            # Кубковый экспресс: матчи одной серии — не независимые события.
            # Исход серии есть функция от её игр, поэтому «П1 в игре 1» + «проход
            # этой же серии» в одном купоне перемножают коэффициенты там, где
            # вероятности складываются, и купон становится арбитражем против
            # модели. Правило шире лигового SGP-запрета намеренно: там соседние
            # туры независимы, здесь одна серия.
            if len(selections) > 1 and match_is_cup(match_row):
                series_id = _row_col(match_row, "cup_series_id")
                if series_id:
                    if series_id in seen_cup_series:
                        return False, {
                            "error": "SERIES_CORRELATED",
                            "series_id": series_id,
                            "match_id": m_id,
                            "message": "В одном экспрессе нельзя совмещать исходы разных игр одной серии кубка "
                                       "и её итога: это не независимые события.",
                        }
                    seen_cup_series.add(series_id)

            # Determine odds value from relational schema or legacy bet_markets
            odd_val = None
            resolved_market_id = mkt_id
            resolved_sel_id = sel_id

            if resolved_market_id and resolved_sel_id:
                cursor.execute("""
                    SELECT ms.odds_value, ms.status as sel_status, m.status as mkt_status
                    FROM market_selections ms
                    JOIN markets m ON ms.market_id = m.id
                    WHERE ms.id = ? AND ms.market_id = ?
                """, (resolved_sel_id, resolved_market_id))
                ms_row = cursor.fetchone()
                if ms_row:
                    if ms_row["mkt_status"] in ("suspended", "closed", "settled", "voided") or ms_row["sel_status"] in ("locked", "suspended", "settled"):
                        return False, f"Рынок на исход '{out_type}' временно приостановлен или закрыт."
                    if ms_row["mkt_status"] in ("open", "active") and ms_row["sel_status"] == "active":
                        odd_val = float(ms_row["odds_value"])
            
            if odd_val is None:
                possible_keys = get_possible_outcome_keys(out_type)
                placeholders = ', '.join(['?'] * len(possible_keys))
                cursor.execute(f"""
                    SELECT ms.id as sel_id, ms.market_id, ms.odds_value, ms.status as sel_status, m.status as mkt_status
                    FROM market_selections ms
                    JOIN markets m ON ms.market_id = m.id
                    WHERE m.match_id = ? AND ms.selection_key IN ({placeholders})
                """, [m_id, *possible_keys])
                ms_match = cursor.fetchone()
                if ms_match:
                    if ms_match["mkt_status"] in ("suspended", "closed", "settled", "voided") or ms_match["sel_status"] in ("locked", "suspended", "settled"):
                        return False, f"Рынок на исход '{out_type}' временно приостановлен или закрыт."
                    if ms_match["mkt_status"] in ("open", "active") and ms_match["sel_status"] == "active":
                        odd_val = float(ms_match["odds_value"])
                        resolved_market_id = ms_match["market_id"]
                        resolved_sel_id = ms_match["sel_id"]

            if odd_val is None and not match_is_cup(match_row) and match_row["status"] != "live":
                cursor.execute("SELECT * FROM bet_markets WHERE match_id = ? AND is_active = 1", (m_id,))
                bm_row = cursor.fetchone()
                if bm_row:
                    col = LEGACY_BET_MARKET_COLUMNS.get(str(out_type).lower())
                    if col and col in bm_row.keys():
                        odd_val = bm_row[col]

            if odd_val is None:
                cup_error = cup_outcome_missing_error(match_row, out_type)
                if cup_error:
                    return False, cup_error
                return False, f"Исход '{out_type}' на матч #{m_id} недоступен или заблокирован."

            odd_val = round(float(odd_val), 2)

            # Phase 5: ODDS_CHANGED detection — client odd vs server odd
            # Server-authoritative: validated across all bets when client provides an expected odd.
            client_odd = s.get("odd")
            if client_odd is not None:
                client_odd_rounded = round(float(client_odd), 2)
                if abs(client_odd_rounded - odd_val) > 0.001:
                    return False, {
                        "error": "ODDS_CHANGED",
                        "match_id": m_id,
                        "outcome": out_type,
                        "old_odd": client_odd_rounded,
                        "new_odd": odd_val,
                        "message": f"Коэффициент изменился: {client_odd_rounded} → {odd_val}"
                    }

            validated_items.append({
                "match_id": m_id,
                "outcome_type": out_type,
                "odd": odd_val,
                "market_id": resolved_market_id,
                "selection_id": resolved_sel_id
            })

        bet_type = "single" if len(validated_items) == 1 else "express"
        # Надбавку купон запоминает: расчёт пойдёт по ней, даже если её потом поменяют.
        margin_pct = get_express_margin_pct() if bet_type == "express" else None
        total_odd = express_odd([it["odd"] for it in validated_items], margin_pct)
        potential_win = int(round(amount * total_odd))

        # Phase 5: MAX_PAYOUT check
        if potential_win > _MAX_PAYOUT:
            max_allowed = int(_MAX_PAYOUT / max(1.01, total_odd))
            return False, {"error": "MAX_PAYOUT_EXCEEDED", "max_payout": _MAX_PAYOUT,
                           "max_allowed_stake": max_allowed,
                           "message": f"Потенциальный выигрыш {potential_win:,} превышает максимум {_MAX_PAYOUT:,} 🪙. "
                                      f"Максимальная ставка при этом кэфе: {max_allowed:,} 🪙."}

        # 1. Deduct coins with strict balance check
        # 1. Insert user_bet with idempotency check (Phase 5: includes idempotency_payload_hash)
        try:
            cursor.execute("""
                INSERT INTO user_bets (user_id, bet_type, amount, total_odd, potential_win, status, idempotency_key, idempotency_payload_hash, express_margin_pct, created_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, datetime('now', '+3 hours'))
            """, (user_id, bet_type, amount, total_odd, potential_win, idempotency_key, _payload_hash, margin_pct))
            bet_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            if idempotency_key:
                cursor.execute(
                    "SELECT id, idempotency_payload_hash FROM user_bets WHERE user_id = ? AND idempotency_key = ?",
                    (user_id, idempotency_key)
                )
                existing = cursor.fetchone()
                if existing:
                    existing_hash = existing["idempotency_payload_hash"]
                    if existing_hash and existing_hash != _payload_hash:
                        return False, {"error": "IDEMPOTENCY_KEY_REUSED",
                                       "message": "Ключ идемпотентности уже использован для другой ставки."}
                    return True, existing["id"]
            raise

        # 2. Deduct coins with strict balance check
        cursor.execute("""
            UPDATE user_wallets 
            SET balance = balance - ?, total_wagered = total_wagered + ?, bets_count = bets_count + 1, updated_at = datetime('now', '+3 hours')
            WHERE user_id = ? AND balance >= ?
        """, (amount, amount, user_id, amount))

        if cursor.rowcount == 0:
            conn.rollback()
            return False, "Недостаточно монет на балансе."

        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        new_balance = cursor.fetchone()["balance"]

        # 3. Insert items
        for item in validated_items:
            cursor.execute("""
                INSERT INTO bet_items (bet_id, match_id, outcome_type, odd, market_id, selection_id, odds_at_placement, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
            """, (
                bet_id,
                item["match_id"],
                item["outcome_type"],
                item["odd"],
                item["market_id"],
                item["selection_id"],
                item["odd"]
            ))

        # 4. Record transaction with balance_after
        cursor.execute("""
            INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
            VALUES (?, ?, 'bet_placed', ?, 'bet', ?, datetime('now', '+3 hours'))
        """, (user_id, -amount, bet_id, new_balance))

        return True, bet_id


def execute_cashout(
    user_id: int,
    bet_id: int,
    idempotency_key: str | None = None
) -> tuple[bool, dict | str]:
    """
    Execute atomic early cashout settlement (Phase 9).
    1. Check if bet is pending and not yet settled (settled_at IS NULL).
    2. Verify matches/markets are still active.
    3. Calculate cashout offer.
    4. Atomically update user_bets (actual_payout = offer, cashout_at = datetime('now', '+3 hours'), settled_at = datetime('now', '+3 hours'), status = 'won').
    5. Credit user_wallets and record coin_transactions with transaction_type = 'cashout'.
    """
    from config import is_global_lockdown_enabled
    if is_global_lockdown_enabled():
        from handlers.base import is_global_admin
        if not is_global_admin(user_id):
            return False, {"error": "LOGOVO_LOCKDOWN", "message": "Logovo.bet временно закрыт для пользователей"}

    with _bet_placement_lock, transaction() as conn:
        cursor = conn.cursor()

        # 1. Fetch bet
        cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (bet_id, user_id))
        bet = cursor.fetchone()
        if not bet:
            return False, {"error": "BET_NOT_FOUND", "message": f"Ставка #{bet_id} не найдена."}

        if bet["settled_at"] is not None or bet["status"] != "pending":
            return False, {"error": "ALREADY_SETTLED", "message": "Ставка уже рассчитана или закрыта."}

        # 2. Fetch bet items
        cursor.execute("""
            SELECT bi.*,
                   ms.odds_value as current_odd,
                   ms.status as sel_status,
                   m.status as market_status,
                   mat.status as match_status
            FROM bet_items bi
            LEFT JOIN market_selections ms ON bi.selection_id = ms.id
            LEFT JOIN markets m ON bi.market_id = m.id
            LEFT JOIN matches mat ON bi.match_id = mat.id
            WHERE bi.bet_id = ?
        """, (bet_id,))
        items = [dict(r) for r in cursor.fetchall()]

        # Resolve current odds fallback if selection_id is NULL
        for it in items:
            if it.get("current_odd") is None:
                cursor.execute("""
                    SELECT ms.odds_value, ms.status as sel_status, m.status as market_status
                    FROM market_selections ms
                    JOIN markets m ON ms.market_id = m.id
                    WHERE m.match_id = ? AND ms.selection_key = ?
                """, (it["match_id"], it["outcome_type"]))
                ms_r = cursor.fetchone()
                if ms_r:
                    it["current_odd"] = float(ms_r["odds_value"])
                    it["sel_status"] = ms_r["sel_status"]
                    it["market_status"] = ms_r["market_status"]
                else:
                    cursor.execute("SELECT * FROM bet_markets WHERE match_id = ? AND is_active = 1", (it["match_id"],))
                    bm_r = cursor.fetchone()
                    if bm_r:
                        bm_map = {
                            "p1": "odd_p1", "x": "odd_x", "p2": "odd_p2",
                            "over_2.5": "odd_tb25", "tb25": "odd_tb25",
                            "under_2.5": "odd_tm25", "tm25": "odd_tm25",
                            "btts_yes": "odd_btts_yes", "btts_no": "odd_btts_no"
                        }
                        col = bm_map.get(it["outcome_type"])
                        if col and bm_r[col]:
                            it["current_odd"] = float(bm_r[col])
                            it["sel_status"] = "active"
                            it["market_status"] = "open"

        for it in items:
            if it.get("match_status") in ("completed", "confirmed", "cancelled"):
                return False, {"error": "MATCH_TERMINAL", "message": "Один или несколько матчей уже завершены."}
            if it.get("market_status") in ("suspended", "closed", "settled") or it.get("sel_status") in ("suspended", "locked", "settled"):
                return False, {"error": "MARKET_UNAVAILABLE", "message": "Рынок временно приостановлен или закрыт."}

        # 3. Calculate cashout offer
        from services.cashout_engine import calculate_cashout_offer
        available, offer, reason = calculate_cashout_offer(
            stake=bet["amount"],
            potential_win=bet["potential_win"],
            items=items,
            express_margin_pct=bet["express_margin_pct"] if "express_margin_pct" in bet.keys() else None
        )

        if not available or offer <= 0:
            return False, {"error": "CASHOUT_UNAVAILABLE", "reason": reason, "message": "Cashout временно недоступен для данного купона."}

        # 4. Atomically settle bet as cashout
        cursor.execute("""
            UPDATE user_bets
            SET status = 'cashed_out',
                actual_payout = ?,
                cashout_at = datetime('now', '+3 hours'),
                settled_at = datetime('now', '+3 hours')
            WHERE id = ? AND settled_at IS NULL AND status = 'pending'
        """, (offer, bet_id))

        if cursor.rowcount == 0:
            return False, {"error": "ALREADY_SETTLED", "message": "Ставка уже рассчитана другим процессом."}

        # 5. Credit user wallet
        get_or_create_wallet(user_id)
        cursor.execute("""
            UPDATE user_wallets
            SET balance = balance + ?,
                total_won = total_won + ?,
                updated_at = datetime('now', '+3 hours')
            WHERE user_id = ?
        """, (offer, offer, user_id))

        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        new_balance = cursor.fetchone()["balance"]

        # 6. Record transaction
        cursor.execute("""
            INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
            VALUES (?, ?, 'cashout', ?, 'bet', ?, datetime('now', '+3 hours'))
        """, (user_id, offer, bet_id, new_balance))

        try:
            from services.settlement_engine import notify_bet_cashed_out
            notify_bet_cashed_out(
                cursor, user_id, bet_id, bet["bet_type"], float(bet["total_odd"] or 1.0),
                bet["amount"], bet["potential_win"] or 0, offer, new_balance,
            )
        except Exception as e:
            logger.warning("Cashout notice for bet #%s skipped: %s", bet_id, e)

        return True, {
            "bet_id": bet_id,
            "status": "cashed_out",
            "cashout_payout": offer,
            "payout": offer,
            "stake": bet["amount"],
            "potential_win": bet["potential_win"],
            "balance": new_balance,
            "message": f"✅ Ставка #{bet_id} успешно закрыта досрочно (+{offer} 🪙)"
        }


def get_user_balance(user_id: int) -> int:
    """Convenience getter for user wallet balance."""
    wallet = get_or_create_wallet(user_id)
    return wallet.get("balance", 0)


def get_user_bet_by_id(arg1: int | None = None, arg2: int | None = None, user_id: int | None = None, bet_id: int | None = None) -> dict | None:
    """
    Fetch a single user prediction slip with its nested legs and status.
    Supports:
      - get_user_bet_by_id(user_id, bet_id)
      - get_user_bet_by_id(bet_id, user_id)
      - get_user_bet_by_id(user_id=user_id, bet_id=bet_id)
      - get_user_bet_by_id(bet_id, user_id=user_id)
    """
    with transaction() as conn:
        cursor = conn.cursor()
        row = None
        if user_id is not None and bet_id is not None:
            cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (bet_id, user_id))
            row = cursor.fetchone()
        elif user_id is not None and arg1 is not None:
            cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (arg1, user_id))
            row = cursor.fetchone()
        elif bet_id is not None and arg1 is not None:
            cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (bet_id, arg1))
            row = cursor.fetchone()
        elif arg1 is not None and arg2 is not None:
            cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (arg2, arg1))
            row = cursor.fetchone()
            if not row:
                cursor.execute("SELECT * FROM user_bets WHERE id = ? AND user_id = ?", (arg1, arg2))
                row = cursor.fetchone()
        else:
            return None

        if not row:
            return None
        bet = dict(row)
        target_bid = bet["id"]
        cursor.execute(
            """
            SELECT bi.*, 
                   COALESCE(m.player1_team, cs.team1_name, bm.team1_name, 'Хозяева') as team1_name,
                   COALESCE(m.player2_team, cs.team2_name, bm.team2_name, 'Гости') as team2_name,
                   COALESCE(m.round_number, bm.tour, 1) as tour,
                   m.tournament_type,
                   cs.stage AS cup_stage,
                   m.game_num_in_series,
                   COALESCE(m.is_series_header, 0) AS is_series_header,
                   m.status as match_status,
                   m.player1_score,
                   m.player2_score,
                   m.ht_score1,
                   m.ht_score2,
                   m.live_minute,
                   mkt.market_name,
                   ms.selection_name
            FROM bet_items bi
            LEFT JOIN matches m ON bi.match_id = m.id
            LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
            LEFT JOIN bet_markets bm ON bi.match_id = bm.match_id
            LEFT JOIN markets mkt ON bi.market_id = mkt.id
            LEFT JOIN market_selections ms ON bi.selection_id = ms.id
            WHERE bi.bet_id = ?
            """,
            (target_bid,)
        )
        bet["items"] = [dict(item) for item in cursor.fetchall()]
        return bet


def get_user_bets(user_id: int, status: str | None = None, limit: int = 20, offset: int = 0) -> list[dict]:
    """Fetch user's prediction slips with rich nested legs and match status."""
    with transaction() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM user_bets WHERE user_id = ?"
        params = [user_id]
        if status and status != "all":
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)

        cursor.execute(query, params)
        bets = [dict(r) for r in cursor.fetchall()]

        for b in bets:
            cursor.execute(
                """
                SELECT bi.*, 
                       COALESCE(m.player1_team, cs.team1_name, bm.team1_name, 'Хозяева') as team1_name,
                       COALESCE(m.player2_team, cs.team2_name, bm.team2_name, 'Гости') as team2_name,
                       COALESCE(m.round_number, bm.tour, 1) as tour,
                       m.tournament_type,
                       cs.stage AS cup_stage,
                       m.game_num_in_series,
                       COALESCE(m.is_series_header, 0) AS is_series_header,
                       m.status as match_status,
                       m.player1_score,
                       m.player2_score,
                       m.ht_score1,
                       m.ht_score2,
                       m.live_minute,
                       mkt.market_name,
                       ms.selection_name
                FROM bet_items bi
                LEFT JOIN matches m ON bi.match_id = m.id
                LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
                LEFT JOIN bet_markets bm ON bi.match_id = bm.match_id
                LEFT JOIN markets mkt ON bi.market_id = mkt.id
                LEFT JOIN market_selections ms ON bi.selection_id = ms.id
                WHERE bi.bet_id = ?
                """,
                (b["id"],)
            )
            b["items"] = [dict(item) for item in cursor.fetchall()]

        return bets


def _label_bet_items(items: list[dict]) -> list[dict]:
    """`cup_label` для ног купона — из колонок этапа, что уже выбраны."""
    for it in items:
        it["cup_label"] = (cup_label_of(it.get("cup_stage_key"), it.get("cup_division_id"),
                                        it.get("cup_division_code"), it.get("cup_division_name"))
                           if it.get("tournament_type") == "cup" else None)
    return items


def get_all_bets(
    status: str | None = None,
    division_id: int | None = None,
    user_id: int | None = None,
    limit: int = 10,
    offset: int = 0,
    division_ids: list[int] | None = None,
) -> tuple[list[dict], int]:
    """Fetch prediction slips across all users (super-admin view) with user details, nested legs and pagination.
    Returns (list_of_bets, total_matching_count).

    `division_ids` scopes the feed to several divisions at once (a division
    admin's set); a match without a division counts as division 1 there.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        where_clauses = ["1=1"]
        params: list = []

        if status and status != "all":
            if status == "refunded":
                where_clauses.append("ub.status IN ('refunded', 'cancelled')")
            else:
                where_clauses.append("ub.status = ?")
                params.append(status)

        if user_id is not None:
            where_clauses.append("ub.user_id = ?")
            params.append(user_id)

        if division_id is not None:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM bet_items bi_d
                    JOIN matches m_d ON bi_d.match_id = m_d.id
                    WHERE bi_d.bet_id = ub.id AND m_d.division_id = ?
                )
            """)
            params.append(division_id)

        if division_ids is not None:
            in_list, div_params = _division_scope(division_ids)
            where_clauses.append(f"""
                EXISTS (
                    SELECT 1 FROM bet_items bi_s
                    JOIN matches m_s ON bi_s.match_id = m_s.id
                    WHERE bi_s.bet_id = ub.id AND COALESCE(m_s.division_id, 1) IN {in_list}
                )
            """)
            params.extend(div_params)

        where_sql = " AND ".join(where_clauses)

        cursor.execute(f"SELECT COUNT(*) as cnt FROM user_bets ub WHERE {where_sql}", params)
        total_count = cursor.fetchone()["cnt"]

        query = f"""
            SELECT ub.*,
                   u.username,
                   u.team_name as user_team,
                   u.league_name as user_league
            FROM user_bets ub
            LEFT JOIN users u ON ub.user_id = u.telegram_id
            WHERE {where_sql}
            ORDER BY ub.id DESC
            LIMIT ? OFFSET ?
        """
        query_params = list(params) + [limit, offset]
        cursor.execute(query, query_params)
        bets = [dict(r) for r in cursor.fetchall()]

        for b in bets:
            cursor.execute(
                """
                SELECT bi.*, 
                       COALESCE(m.player1_team, cs.team1_name, bm.team1_name, 'Хозяева') as team1_name,
                       COALESCE(m.player2_team, cs.team2_name, bm.team2_name, 'Гости') as team2_name,
                       COALESCE(m.round_number, bm.tour, 1) as tour,
                       m.tournament_type,
                       cs.stage AS cup_stage,
                       m.game_num_in_series,
                       COALESCE(m.is_series_header, 0) AS is_series_header,
                       m.division_id,
                       d.name as division_name,
                       d.code as division_code,
                       st.stage AS cup_stage_key,
                       st.division_id AS cup_division_id,
                       cd.code AS cup_division_code,
                       cd.name AS cup_division_name,
                       m.status as match_status,
                       m.player1_score,
                       m.player2_score,
                       m.ht_score1,
                       m.ht_score2,
                       m.live_minute,
                       mkt.market_name,
                       mkt.market_key,
                       ms.selection_name
                FROM bet_items bi
                LEFT JOIN matches m ON bi.match_id = m.id
                LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
                LEFT JOIN divisions d ON m.division_id = d.id
                LEFT JOIN cup_stages st ON st.id = COALESCE(m.stage_id, cs.stage_id)
                LEFT JOIN divisions cd ON cd.id = st.division_id
                LEFT JOIN bet_markets bm ON bi.match_id = bm.match_id
                LEFT JOIN markets mkt ON bi.market_id = mkt.id
                LEFT JOIN market_selections ms ON bi.selection_id = ms.id
                WHERE bi.bet_id = ?
                """,
                (b["id"],)
            )
            b["items"] = _label_bet_items([dict(item) for item in cursor.fetchall()])

        return bets, total_count


def get_bets_summary_stats(division_id: int | None = None) -> dict:
    """Get high-level summary KPIs and bookmaker metrics across all bets."""
    with transaction() as conn:
        cursor = conn.cursor()
        where_sql = ""
        params = []
        if division_id is not None:
            where_sql = """
                WHERE EXISTS (
                    SELECT 1 FROM bet_items bi_d
                    JOIN matches m_d ON bi_d.match_id = m_d.id
                    WHERE bi_d.bet_id = ub.id AND m_d.division_id = ?
                )
            """
            params.append(division_id)

        query = f"""
            SELECT
                COUNT(ub.id) as total_bets,
                SUM(CASE WHEN ub.status = 'pending' THEN 1 ELSE 0 END) as count_pending,
                SUM(CASE WHEN ub.status = 'won' THEN 1 ELSE 0 END) as count_won,
                SUM(CASE WHEN ub.status = 'lost' THEN 1 ELSE 0 END) as count_lost,
                SUM(CASE WHEN ub.status IN ('refunded', 'cancelled') THEN 1 ELSE 0 END) as count_refunded,
                SUM(CASE WHEN ub.status = 'cashed_out' THEN 1 ELSE 0 END) as count_cashed_out,
                COALESCE(SUM(ub.amount), 0) as total_wagered,
                COALESCE(SUM(CASE WHEN ub.status = 'pending' THEN ub.amount ELSE 0 END), 0) as pending_exposure,
                COALESCE(SUM(CASE WHEN ub.status = 'pending' THEN ub.potential_win ELSE 0 END), 0) as pending_potential_liability,
                COALESCE(SUM(CASE WHEN ub.status IN ('won', 'cashed_out') THEN ub.actual_payout ELSE 0 END), 0) as total_paid_out
            FROM user_bets ub
            {where_sql}
        """
        cursor.execute(query, params)
        row = cursor.fetchone()
        if not row:
            return {
                "total_bets": 0,
                "count_pending": 0,
                "count_won": 0,
                "count_lost": 0,
                "count_refunded": 0,
                "count_cashed_out": 0,
                "total_wagered": 0,
                "pending_exposure": 0,
                "pending_potential_liability": 0,
                "total_paid_out": 0,
                "bookmaker_profit": 0,
            }

        res = dict(row)
        for k in ("total_bets", "count_pending", "count_won", "count_lost", "count_refunded", "count_cashed_out",
                  "total_wagered", "pending_exposure", "pending_potential_liability", "total_paid_out"):
            res[k] = res.get(k) or 0

        res["bookmaker_profit"] = res["total_wagered"] - res["total_paid_out"]
        return res


def get_bet_by_id(bet_id: int) -> dict | None:
    """Fetch any bet by ID (admin view) with user info, wallet balance, and nested legs."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ub.*,
                   u.username,
                   u.team_name as user_team,
                   u.league_name as user_league,
                   w.balance as user_wallet_balance
            FROM user_bets ub
            LEFT JOIN users u ON ub.user_id = u.telegram_id
            LEFT JOIN user_wallets w ON ub.user_id = w.user_id
            WHERE ub.id = ?
        """, (bet_id,))
        row = cursor.fetchone()
        if not row:
            return None
        bet = dict(row)
        cursor.execute(
            """
            SELECT bi.*, 
                   COALESCE(m.player1_team, cs.team1_name, bm.team1_name, 'Хозяева') as team1_name,
                   COALESCE(m.player2_team, cs.team2_name, bm.team2_name, 'Гости') as team2_name,
                       COALESCE(m.round_number, bm.tour, 1) as tour,
                       m.tournament_type,
                       cs.stage AS cup_stage,
                       m.game_num_in_series,
                       COALESCE(m.is_series_header, 0) AS is_series_header,
                       m.division_id,
                       d.name as division_name,
                       d.code as division_code,
                       st.stage AS cup_stage_key,
                       st.division_id AS cup_division_id,
                       cd.code AS cup_division_code,
                       cd.name AS cup_division_name,
                       m.status as match_status,
                       m.player1_score,
                       m.player2_score,
                       m.ht_score1,
                       m.ht_score2,
                       m.live_minute,
                       mkt.market_name,
                       mkt.market_key,
                       ms.selection_name
                FROM bet_items bi
                LEFT JOIN matches m ON bi.match_id = m.id
                LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
                LEFT JOIN divisions d ON m.division_id = d.id
                LEFT JOIN cup_stages st ON st.id = COALESCE(m.stage_id, cs.stage_id)
                LEFT JOIN divisions cd ON cd.id = st.division_id
                LEFT JOIN bet_markets bm ON bi.match_id = bm.match_id
                LEFT JOIN markets mkt ON bi.market_id = mkt.id
                LEFT JOIN market_selections ms ON bi.selection_id = ms.id
                WHERE bi.bet_id = ?
                """,
            (bet_id,)
        )
        bet["items"] = _label_bet_items([dict(item) for item in cursor.fetchall()])
        return bet


def get_user_bet_summary(user_id: int) -> dict:
    """Betting track record of one user for the admin bet card.

    net_profit counts only settled slips: payouts of won/cashed-out slips minus
    their stakes and the stakes of lost ones. Refunds net to zero, pending
    slips are reported separately as pending_amount.
    """
    with transaction() as conn:
        row = conn.execute("""
            SELECT
                COUNT(*) AS total_bets,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS count_pending,
                SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) AS count_won,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) AS count_lost,
                SUM(CASE WHEN status IN ('refunded', 'cancelled') THEN 1 ELSE 0 END) AS count_refunded,
                SUM(CASE WHEN status = 'cashed_out' THEN 1 ELSE 0 END) AS count_cashed_out,
                COALESCE(SUM(amount), 0) AS total_wagered,
                COALESCE(SUM(CASE WHEN status = 'pending' THEN amount ELSE 0 END), 0) AS pending_amount,
                COALESCE(SUM(CASE WHEN status IN ('won', 'cashed_out') THEN actual_payout ELSE 0 END), 0) AS total_paid_out,
                COALESCE(SUM(CASE WHEN status IN ('won', 'lost', 'cashed_out') THEN amount ELSE 0 END), 0) AS settled_wagered,
                MAX(CASE WHEN status = 'won' THEN actual_payout END) AS best_payout
            FROM user_bets
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    res = {k: (row[k] or 0) for k in row.keys()}
    res["net_profit"] = res["total_paid_out"] - res["settled_wagered"]
    decided = res["count_won"] + res["count_lost"]
    res["win_rate"] = round(res["count_won"] * 100.0 / decided, 1) if decided else None
    return res


LIVE_BET_ALERTS_KEY = "live_bet_alert_subscribers"


def is_live_bet_alerts_enabled(admin_id: int) -> bool:
    """Check if admin is subscribed to live bet alerts in PM."""
    subs = get_live_bet_alert_subscribers()
    return admin_id in subs


def set_live_bet_alerts_enabled(admin_id: int, enabled: bool) -> None:
    """Subscribe or unsubscribe admin from live bet alerts in PM."""
    subs = set(get_live_bet_alert_subscribers())
    if enabled:
        subs.add(admin_id)
    else:
        subs.discard(admin_id)
    raw = ",".join(str(i) for i in sorted(subs))
    set_config(LIVE_BET_ALERTS_KEY, raw)


def get_live_bet_alert_subscribers() -> list[int]:
    """Return list of admin IDs subscribed to live bet alerts."""
    val = get_config(LIVE_BET_ALERTS_KEY)
    if not val:
        return []
    result = []
    for item in val.split(","):
        item = item.strip()
        if item.isdigit():
            result.append(int(item))
    return result


# ─── Phase 5: Betting Audit Log ──────────────────────────────────────────────

def log_betting_audit(
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    old_value: "dict | str | None" = None,
    new_value: "dict | str | None" = None,
    division_id: "int | None" = None,
    season_id: "int | None" = None,
) -> int:
    """
    Write a betting audit log entry to bet_audit_log.
    Returns the new log entry ID.
    """
    import json as _json
    old_v = _json.dumps(old_value, ensure_ascii=False) if old_value is not None else None
    new_v = _json.dumps(new_value, ensure_ascii=False) if new_value is not None else None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO bet_audit_log (actor_id, action, entity_type, entity_id, old_value, new_value, division_id, season_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            """,
            (actor_id, action, entity_type, entity_id, old_v, new_v, division_id, season_id),
        )
        return cursor.lastrowid


write_bet_audit_log = log_betting_audit


# Market lifecycle valid transitions (Phase 5)
# Market lifecycle valid transitions — aligned with DB CHECK constraint:
# markets.status IN ('open','suspended','closed','settled','voided')
# 'voided' доступна и из живых статусов: аннулировать рынок нужно до закрытия
# линии (отмена матча, ошибочная роспись), а close → voided для этого не ждём.
_MARKET_TRANSITIONS: dict[str, set[str]] = {
    "open":      {"suspended", "closed", "voided"},
    "suspended": {"open", "closed", "voided"},
    "closed":    {"settled", "voided"},
    "settled":   set(),   # terminal state
    "voided":    set(),   # terminal state
}


def transition_market_status(market_id: int, new_status: str, actor_id: int) -> dict:
    """
    Atomically transition a market to a new lifecycle status.
    Enforces the state machine: open→suspended→open→closed→settled/voided.
    Status values must match DB CHECK: open, suspended, closed, settled, voided.
    Returns dict with id, old_status, new_status.
    Raises ValueError for forbidden transitions.
    """
    allowed_statuses = {"open", "suspended", "closed", "settled", "voided"}
    if new_status not in allowed_statuses:
        raise ValueError(f"Invalid market status: '{new_status}'")

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, status, match_id FROM markets WHERE id = ?", (market_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Market #{market_id} not found.")

        current_status = row["status"]
        allowed_next = _MARKET_TRANSITIONS.get(current_status, set())

        if new_status not in allowed_next:
            raise ValueError(
                f"Forbidden market transition: {current_status!r} → {new_status!r} for market #{market_id}."
            )

        cursor.execute(
            "UPDATE markets SET status = ? WHERE id = ?",
            (new_status, market_id),
        )

        # Fetch division_id via match
        division_id = None
        cursor.execute("SELECT division_id FROM matches WHERE id = ?", (row["match_id"],))
        m = cursor.fetchone()
        if m:
            division_id = m["division_id"]

    # Log outside of above transaction to avoid nested transaction issues with log_betting_audit
    log_betting_audit(
        actor_id=actor_id,
        action=f"market_{new_status}",
        entity_type="market",
        entity_id=market_id,
        old_value={"status": current_status},
        new_value={"status": new_status},
        division_id=division_id,
    )

    return {"id": market_id, "old_status": current_status, "new_status": new_status}


def update_selection_odds(selection_id: int, new_odd: float, actor_id: int) -> dict:
    """
    Update a market selection's odds, increment odds_version, store previous_odds, and log audit.
    Returns dict with selection_id, old_odd, new_odd.
    """
    if new_odd <= 1.00:
        raise ValueError("Odds must be greater than 1.00.")

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, odds_value, market_id FROM market_selections WHERE id = ?",
            (selection_id,),
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Selection #{selection_id} not found.")

        old_odd = float(row["odds_value"])
        new_odd = round(float(new_odd), 2)

        cursor.execute(
            """
            UPDATE market_selections
            SET previous_odds = odds_value,
                odds_value = ?,
                odds_version = odds_version + 1,
                updated_at = datetime('now', '+3 hours')
            WHERE id = ?
            """,
            (new_odd, selection_id),
        )

        # Also record in odds_history if table exists
        try:
            cursor.execute(
                """
                INSERT INTO odds_history (selection_id, old_value, new_value, changed_by, changed_at)
                VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))
                """,
                (selection_id, old_odd, new_odd, actor_id),
            )
        except Exception:
            pass  # odds_history may have different schema — skip silently

    log_betting_audit(
        actor_id=actor_id,
        action="odds_changed",
        entity_type="selection",
        entity_id=selection_id,
        old_value={"odds_value": old_odd},
        new_value={"odds_value": new_odd},
    )

    return {"selection_id": selection_id, "old_odd": old_odd, "new_odd": new_odd}


def void_user_bet(bet_id: int, actor_id: int) -> dict:
    """
    Void a user bet and refund the stake.
    Only pending/won/lost bets that have not already been voided can be voided.
    Returns dict with bet_id, refunded_amount, user_id.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_bets WHERE id = ?", (bet_id,))
        bet = cursor.fetchone()
        if not bet:
            raise ValueError(f"Bet #{bet_id} not found.")

        if bet["status"] != "pending":
            raise ValueError(f"Bet #{bet_id} cannot be voided because it is already {bet['status']}.")

        stake = bet["amount"]
        user_id = bet["user_id"]

        # Void the bet and all its items
        cursor.execute(
            "UPDATE user_bets SET status = 'refunded', actual_payout = ?, settled_at = datetime('now', '+3 hours') WHERE id = ? AND status = 'pending' AND settled_at IS NULL",
            (stake, bet_id),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"Bet #{bet_id} could not be voided (already settled or refunded).")

        cursor.execute(
            "UPDATE bet_items SET status = 'refunded' WHERE bet_id = ? AND status = 'pending'",
            (bet_id,),
        )

        # Refund stake
        get_or_create_wallet(user_id)
        cursor.execute(
            "UPDATE user_wallets SET balance = balance + ?, updated_at = datetime('now', '+3 hours') WHERE user_id = ?",
            (stake, user_id),
        )
        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        bal_after = cursor.fetchone()["balance"]

        cursor.execute(
            """
            INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
            VALUES (?, ?, 'admin_refund', ?, 'bet', ?, datetime('now', '+3 hours'))
            """,
            (user_id, stake, bet_id, bal_after),
        )

    log_betting_audit(
        actor_id=actor_id,
        action="bet_voided",
        entity_type="bet",
        entity_id=bet_id,
        old_value={"status": bet["status"], "amount": stake},
        new_value={"status": "refunded", "refund": stake},
    )

    return {"bet_id": bet_id, "user_id": user_id, "refunded_amount": stake}


def void_market(market_id: int, actor_id: int, reason: str) -> dict:
    """
    Аннулировать рынок и разобрать все затронутые купоны по одним и тем же
    правилам, по которым это делает расчёт.

    Нога аннулированного рынка становится 'refunded' (CHECK `bet_items` не
    знает статуса 'voided', см. settlement_engine). После этого:

    - живая (pending) нога осталась — купон ждёт свой расчёт: settlement
      перемноживает только 'won'-ноги, поэтому аннулированная нога сама
      даёт 1.00 и выплата считается правильно;
    - pending-ног не осталось — купон мёртв, и закрывает его канонический
      владелец админского аннулирования `void_user_bet` (полный возврат
      стейка, реестр 'admin_refund'/'bet', аудит). Своей формулы выплаты
      здесь намеренно нет.

    Идемпотентность: повторный вызов на 'voided' рынке ничего не меняет и
    ничего не возвращает второй раз; на 'settled' — ValueError.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, status, match_id FROM markets WHERE id = ?", (market_id,))
        market = cursor.fetchone()
        if not market:
            raise ValueError(f"Market #{market_id} not found.")
        if market["status"] == "settled":
            raise ValueError(f"Market #{market_id} is already settled and cannot be voided.")
        old_status = market["status"]

        cursor.execute("SELECT division_id, season_id FROM matches WHERE id = ?", (market["match_id"],))
        match_row = cursor.fetchone()
        div_id = match_row["division_id"] if match_row else None
        season_id = match_row["season_id"] if match_row else None

        if market["status"] == "voided":
            return {
                "market_id": market_id, "old_status": "voided", "already_voided": True,
                "voided_legs": 0, "refunded_bets": [], "refunded_stake": 0,
                "pending_coupons": 0, "division_id": div_id, "season_id": season_id,
            }

        transition_market_status(market_id, "voided", actor_id)

        cursor.execute("""
            SELECT DISTINCT ub.id, ub.user_id, ub.bet_type, ub.amount
            FROM user_bets ub
            JOIN bet_items bi ON bi.bet_id = ub.id
            WHERE bi.market_id = ? AND ub.status = 'pending'
            ORDER BY ub.id
        """, (market_id,))
        coupons = [dict(r) for r in cursor.fetchall()]

        voided_legs = 0
        refunded_bets = []
        pending_coupons = 0
        for coupon in coupons:
            cursor.execute("""
                UPDATE bet_items SET status = 'refunded'
                WHERE bet_id = ? AND market_id = ? AND status = 'pending'
            """, (coupon["id"], market_id))
            voided_legs += cursor.rowcount

            cursor.execute(
                "SELECT 1 FROM bet_items WHERE bet_id = ? AND status = 'pending' LIMIT 1",
                (coupon["id"],),
            )
            if cursor.fetchone():
                pending_coupons += 1
                continue

            refund = void_user_bet(coupon["id"], actor_id)
            cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (coupon["user_id"],))
            balance_after = cursor.fetchone()["balance"]
            write_bet_audit_log(
                actor_id=actor_id,
                action="market_void_bet_refund",
                entity_type="bet",
                entity_id=coupon["id"],
                old_value={"status": "pending"},
                new_value={"status": "refunded", "refund": refund["refunded_amount"],
                           "reason": reason, "market_id": market_id},
                division_id=div_id,
                season_id=season_id,
            )
            refunded_bets.append({
                "bet_id": coupon["id"],
                "user_id": coupon["user_id"],
                "bet_type": coupon["bet_type"],
                "stake": refund["refunded_amount"],
                "balance_after": balance_after,
            })

        market_summary = {
            "market_id": market_id,
            "old_status": old_status,
            "already_voided": False,
            "voided_legs": voided_legs,
            "refunded_bets": refunded_bets,
            "refunded_stake": sum(b["stake"] for b in refunded_bets),
            "pending_coupons": pending_coupons,
            "division_id": div_id,
            "season_id": season_id,
        }

    # Уведомление, рейтинг и стрики — постфактум и не в транзакции возврата:
    # сбой оповещения не должен отменять уже зачисленные монеты. Тот же
    # комплект сайд-эффектов, что у ветки all_voided в settlement_engine.
    if refunded_bets:
        try:
            from services.settlement_engine import notify_bet_refunded
            from services.player_rating import PlayerRatingEngine
            from services.streak_engine import StreakEngine
            from services.leaderboard_service import invalidate_leaderboard_cache
        except Exception:
            logger.exception("MARKET_VOID_SIDE_EFFECTS_UNAVAILABLE: refunds are committed "
                             f"on market #{market_id}, notices/ratings were skipped")
        else:
            for entry in market_summary["refunded_bets"]:
                try:
                    with transaction() as notice_conn:
                        notify_bet_refunded(notice_conn.cursor(), entry["user_id"], entry["bet_id"],
                                             entry["bet_type"], entry["stake"], entry["balance_after"])
                    PlayerRatingEngine.process_bet_settlement(
                        user_id=entry["user_id"], outcome="refunded",
                        total_odd=1.0, stake=entry["stake"], payout=entry["stake"]
                    )
                    StreakEngine.process_bet_outcome(entry["user_id"], "refunded")
                except Exception:
                    logger.exception("MARKET_VOID_SIDE_EFFECT_FAILED: bet #%s was refunded "
                                     f"on market #{market_id}", entry["bet_id"])
            try:
                invalidate_leaderboard_cache()
            except Exception:
                logger.warning("Could not invalidate leaderboard cache after voiding market #%s", market_id)

    return market_summary


def get_betting_audit_log(
    limit: int = 50,
    offset: int = 0,
    actor_id: "int | None" = None,
    entity_type: "str | None" = None,
    division_id: "int | None" = None,
) -> list[dict]:
    """Fetch betting audit log entries with optional filters."""
    with transaction() as conn:
        cursor = conn.cursor()
        query = "SELECT * FROM bet_audit_log WHERE 1=1"
        params: list = []
        if actor_id is not None:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if entity_type is not None:
            query += " AND entity_type = ?"
            params.append(entity_type)
        if division_id is not None:
            query += " AND division_id = ?"
            params.append(division_id)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


# ═══ Logovo.bet: админ-панель в Mini App ═════════════════════════════════════
#
# То, чего вкладке «Управление» не хватало в уже существующих /api/admin/*:
# запрет ставок отдельному игроку, экстренная остановка приёма, ручная
# корректировка баланса, поиск игроков и сводка букмекера.

MIGRATION_024_BETTING_BANS = "024_betting_bans"
BETTING_PAUSE_KEY = "betting_pause"
BETTING_BANNED_ERROR = "BETTING_BANNED"
BETTING_PAUSED_ERROR = "BETTING_PAUSED"
# Потолок одной ручной корректировки: опечатка в лишний ноль не должна
# вливать в закрытую экономику миллионы (стартовый кошелёк — 677 🪙).
ADMIN_WALLET_ADJUST_MAX = 100_000


def _ensure_betting_bans(cursor: sqlite3.Cursor) -> None:
    """Миграция 024: одна строка на игрока, которому запрещены ставки.

    Истории в таблице нет — снятие запрета удаляет строку, а кто, когда и
    почему запрещал, остаётся в `bet_audit_log`. Внешнего ключа на `users` нет
    сознательно: кошелёк бывает и у того, кто не зарегистрирован тренером.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS betting_bans (
            user_id INTEGER PRIMARY KEY,
            reason TEXT,
            banned_by INTEGER NOT NULL,
            banned_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
        )
    """)
    cursor.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (MIGRATION_024_BETTING_BANS, "betting_bans: per-player betting ban set from the admin panel"),
    )


MIGRATION_025_DROP_DAILY_BONUS = "025_drop_daily_bonus_setting"


def _drop_daily_bonus_setting(cursor: sqlite3.Cursor) -> None:
    """Миграция 025: удалить сумму ежедневного бонуса, заданную в панели.

    Бонус убран целиком, и строку `daily_bonus` в `risk_limits_config` больше
    никто не читает — она только висела бы в `global_overrides`. История
    начислений (`coin_transactions` с типом `daily_bonus`) не трогается.
    """
    cursor.execute("SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION_025_DROP_DAILY_BONUS,))
    if cursor.fetchone():
        return
    cursor.execute("DELETE FROM risk_limits_config WHERE limit_key = 'daily_bonus'")
    if cursor.rowcount:
        logger.info("Migration 025: removed %s daily_bonus setting row(s)", cursor.rowcount)
    cursor.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (MIGRATION_025_DROP_DAILY_BONUS, "Drop the daily_bonus panel setting after the bonus was removed"),
    )


def _parse_betting_pause(raw: str | None) -> dict:
    """Состояние остановки приёма: {'global': entry|None, 'divisions': {id: entry}}.

    Битый JSON — это исключение, а не «пауз нет»: `place_user_bet` превращает
    его в отказ (fail-closed), а новая запись паузы перезаписывает состояние.
    """
    state = {"global": None, "divisions": {}}
    if not raw:
        return state
    data = json.loads(raw)
    state["global"] = data.get("global") or None
    for key, entry in (data.get("divisions") or {}).items():
        if entry:
            state["divisions"][int(key)] = entry
    return state


def _read_betting_pause(cursor: sqlite3.Cursor) -> dict:
    cursor.execute("SELECT value FROM system_config WHERE key = ?", (BETTING_PAUSE_KEY,))
    row = cursor.fetchone()
    return _parse_betting_pause(row["value"] if row else None)


def get_betting_pause() -> dict:
    """Текущая экстренная остановка приёма ставок (глобальная и по дивизионам)."""
    with transaction() as conn:
        return _read_betting_pause(conn.cursor())


def set_betting_pause(actor_id: int, paused: bool, division_id: int | None = None,
                      reason: str | None = None) -> dict:
    """Остановить или возобновить приём новых купонов — везде или в одном дивизионе.

    Рынки при этом не трогаются: остановка действует на входе в
    `place_user_bet`, поэтому её не может молча отменить синхронизация линии,
    которая переоткрывает приостановленные рынки. Уже принятые купоны
    рассчитываются как обычно.
    """
    entry = ({"reason": (reason or "").strip() or None, "by": actor_id, "at": now_msk_str()}
             if paused else None)
    with transaction() as conn:
        cursor = conn.cursor()
        try:
            state = _read_betting_pause(cursor)
        except (ValueError, TypeError, AttributeError):
            logger.warning("Corrupt betting pause state was overwritten by actor %s", actor_id)
            state = {"global": None, "divisions": {}}

        if division_id is None:
            old = state["global"]
            state["global"] = entry
        else:
            old = state["divisions"].get(division_id)
            if entry:
                state["divisions"][division_id] = entry
            else:
                state["divisions"].pop(division_id, None)

        stored = {"global": state["global"],
                  "divisions": {str(k): v for k, v in state["divisions"].items()}}
        cursor.execute(
            "REPLACE INTO system_config (key, value) VALUES (?, ?)",
            (BETTING_PAUSE_KEY, json.dumps(stored, ensure_ascii=False)),
        )
        log_betting_audit(
            actor_id=actor_id,
            action="betting_paused" if paused else "betting_resumed",
            entity_type="betting",
            entity_id=division_id or 0,
            old_value=old,
            new_value=entry,
            division_id=division_id,
        )
    return state


def _betting_block_reason(cursor: sqlite3.Cursor, user_id: int, match_ids: list[int]) -> dict | None:
    """Причина, по которой купон не принимается ещё до лимитов и баланса, или None.

    Порядок: персональный запрет → глобальная остановка → остановка дивизиона
    одного из матчей купона (матч без дивизиона — это дивизион 1).
    """
    cursor.execute("SELECT reason FROM betting_bans WHERE user_id = ?", (user_id,))
    ban = cursor.fetchone()
    if ban:
        message = "Ставки для вашего аккаунта отключены администратором."
        if ban["reason"]:
            message += f" Причина: {ban['reason']}"
        return {"error": BETTING_BANNED_ERROR, "message": message}

    state = _read_betting_pause(cursor)
    if state["global"]:
        return {"error": BETTING_PAUSED_ERROR,
                "message": "Приём ставок временно остановлен администратором."}

    if state["divisions"] and match_ids:
        placeholders = ",".join("?" * len(match_ids))
        cursor.execute(
            f"SELECT DISTINCT COALESCE(division_id, 1) AS div FROM matches WHERE id IN ({placeholders})",
            list(match_ids),
        )
        for row in cursor.fetchall():
            if row["div"] in state["divisions"]:
                return {"error": BETTING_PAUSED_ERROR, "division_id": row["div"],
                        "message": "Приём ставок на матчи этого дивизиона временно остановлен."}
    return None


def _betting_player_exists(cursor: sqlite3.Cursor, user_id: int) -> bool:
    cursor.execute(
        "SELECT 1 FROM users WHERE telegram_id = ? UNION SELECT 1 FROM user_wallets WHERE user_id = ?",
        (user_id, user_id),
    )
    return cursor.fetchone() is not None


def get_betting_ban(user_id: int) -> dict | None:
    with transaction() as conn:
        row = conn.execute(
            "SELECT user_id, reason, banned_by, banned_at FROM betting_bans WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def set_betting_ban(user_id: int, actor_id: int, reason: str | None = None) -> dict:
    """Запретить игроку новые ставки. Открытые купоны остаются и рассчитываются."""
    reason = (reason or "").strip() or None
    with transaction() as conn:
        cursor = conn.cursor()
        if not _betting_player_exists(cursor, user_id):
            raise ValueError(f"Игрок #{user_id} не найден.")
        cursor.execute("SELECT reason, banned_at FROM betting_bans WHERE user_id = ?", (user_id,))
        old = cursor.fetchone()
        cursor.execute(
            """
            INSERT INTO betting_bans (user_id, reason, banned_by, banned_at)
            VALUES (?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(user_id) DO UPDATE SET
                reason = excluded.reason,
                banned_by = excluded.banned_by,
                banned_at = excluded.banned_at
            """,
            (user_id, reason, actor_id),
        )
        log_betting_audit(
            actor_id=actor_id,
            action="player_betting_banned",
            entity_type="user",
            entity_id=user_id,
            old_value=dict(old) if old else None,
            new_value={"reason": reason},
        )
    return get_betting_ban(user_id)


def lift_betting_ban(user_id: int, actor_id: int) -> bool:
    """Снять запрет ставок. False — запрета и не было."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT reason, banned_at FROM betting_bans WHERE user_id = ?", (user_id,))
        old = cursor.fetchone()
        if not old:
            return False
        cursor.execute("DELETE FROM betting_bans WHERE user_id = ?", (user_id,))
        log_betting_audit(
            actor_id=actor_id,
            action="player_betting_unbanned",
            entity_type="user",
            entity_id=user_id,
            old_value=dict(old),
            new_value=None,
        )
    return True


def admin_adjust_wallet(user_id: int, amount: int, actor_id: int, reason: str) -> dict:
    """Ручное начисление (amount > 0) или списание (amount < 0) монет.

    В отличие от `deduct_coins`, не трогает `total_wagered`: списание — это не
    ставка, и оборот игрока от него расти не должен. Баланс в минус не уходит.
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount == 0:
        raise ValueError("Сумма корректировки — ненулевое целое число.")
    if abs(amount) > ADMIN_WALLET_ADJUST_MAX:
        raise ValueError(f"За один раз можно изменить баланс не больше чем на {ADMIN_WALLET_ADJUST_MAX:,} 🪙.")
    reason = (reason or "").strip()
    if not reason:
        raise ValueError("Укажите причину корректировки.")

    with transaction() as conn:
        cursor = conn.cursor()
        if not _betting_player_exists(cursor, user_id):
            raise ValueError(f"Игрок #{user_id} не найден.")
        get_or_create_wallet(user_id)
        cursor.execute("SELECT balance FROM user_wallets WHERE user_id = ?", (user_id,))
        old_balance = int(cursor.fetchone()["balance"])
        if old_balance + amount < 0:
            raise ValueError(f"Нельзя списать больше баланса ({old_balance:,} 🪙).")

        cursor.execute(
            "UPDATE user_wallets SET balance = balance + ?, updated_at = datetime('now', '+3 hours') WHERE user_id = ?",
            (amount, user_id),
        )
        new_balance = old_balance + amount
        tx_type = "admin_credit" if amount > 0 else "admin_debit"
        cursor.execute(
            """
            INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, reference_type, balance_after, created_at)
            VALUES (?, ?, ?, ?, 'admin', ?, datetime('now', '+3 hours'))
            """,
            (user_id, amount, tx_type, actor_id, new_balance),
        )
        tx_id = cursor.lastrowid
        log_betting_audit(
            actor_id=actor_id,
            action=f"wallet_{tx_type}",
            entity_type="user",
            entity_id=user_id,
            old_value={"balance": old_balance},
            new_value={"balance": new_balance, "amount": amount, "reason": reason},
        )

    return {"user_id": user_id, "amount": amount, "old_balance": old_balance,
            "new_balance": new_balance, "transaction_id": tx_id, "transaction_type": tx_type}


def get_coin_transactions(user_id: int, limit: int = 30, offset: int = 0) -> list[dict]:
    with transaction() as conn:
        rows = conn.execute(
            """
            SELECT id, amount, transaction_type, reference_id, reference_type, balance_after, created_at
            FROM coin_transactions
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (user_id, limit, offset),
        ).fetchall()
        return [dict(r) for r in rows]


def get_risk_limit_overrides(scope_type: str, scope_id: int) -> dict[str, int]:
    """Лимиты, заданные именно на этом уровне (без наследования сверху)."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT limit_key, limit_value FROM risk_limits_config WHERE scope_type = ? AND scope_id = ?",
            (scope_type, scope_id),
        ).fetchall()
        return {r["limit_key"]: int(r["limit_value"]) for r in rows}


def get_global_limit(limit_key: str, default: int) -> int:
    """Глобальная настройка, заданная в панели, или default, если её не трогали.

    Сбой чтения — тоже default: настройка не должна валить ставку или кошелёк.
    """
    try:
        with transaction() as conn:
            row = conn.execute(
                "SELECT limit_value FROM risk_limits_config"
                " WHERE scope_type = 'global' AND scope_id = 0 AND limit_key = ?",
                (limit_key,),
            ).fetchone()
    except sqlite3.Error:
        logger.warning("Could not read global limit %s, using the default", limit_key, exc_info=True)
        return default
    if row is None or row["limit_value"] is None:
        return default
    return int(row["limit_value"])


def get_max_express_events() -> int:
    return get_global_limit("max_express_events", MAX_EXPRESS_EVENTS)


def get_express_margin_pct() -> int:
    pct = get_global_limit("express_margin_pct", EXPRESS_MARGIN_PCT)
    return max(0, min(MAX_EXPRESS_MARGIN_PCT, pct))


def get_initial_wallet_balance() -> int:
    return get_global_limit("initial_balance", INITIAL_WALLET_BALANCE)


# ─── Запреты на виды ставок ───────────────────────────────────────────────────
# Запрет — это обычная строка `risk_limits_config` с ключом `ban_<группа>` и
# значением 1: глобально (scope global/0) или на дивизион (scope division/id).
# Действуют оба уровня сразу — запрет дивизиона добавляется к глобальным, снять
# глобальный на уровне дивизиона нельзя. Кубок дивизиона подчиняется запретам
# своего дивизиона (через `cup_stages.division_id`), общий кубок — только
# глобальным.
BET_BAN_PREFIX = "ban_"
# группа → (подпись, market_key). Синонимы ключей — те же, что понимает
# `market_settler`: live-провайдер пишет исход как `match_result`. `express`
# рынков не имеет: он запрещает купон из нескольких событий, если в нём есть
# матч под запретом.
BET_BAN_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "result": ("Исход", ("1x2", "match_winner", "outcome", "match_result")),
    "double": ("Двойной шанс", ("double_chance",)),
    "total": ("Тотал", ("total_goals", "totals", "over_under")),
    "itotal": ("Инд. тотал", ("individual_total_1", "individual_total_2")),
    "handicap": ("Фора", ("handicap",)),
    "btts": ("Обе забьют", ("btts", "both_teams_to_score")),
    "correct_score": ("Точный счёт", ("correct_score",)),
    "express": ("Экспресс", ()),
}
BET_BAN_KEYS = tuple(BET_BAN_PREFIX + g for g in BET_BAN_GROUPS)
_BAN_GROUP_BY_MARKET_KEY = {
    key: group for group, (_label, keys) in BET_BAN_GROUPS.items() for key in keys
}
# Исход без известного рынка (старая схема `bet_markets`, клиент без market_id):
# группа по самому ключу исхода. Порядок важен только внутри префиксов — они не
# пересекаются.
_BAN_GROUP_BY_OUTCOME_PREFIX = (
    ("it1_", "itotal"), ("it2_", "itotal"),
    ("h1_", "handicap"), ("h2_", "handicap"),
    ("over_", "total"), ("under_", "total"),
    ("btts_", "btts"), ("cs_", "correct_score"),
)
_BAN_GROUP_BY_OUTCOME = {"p1": "result", "x": "result", "p2": "result",
                         "home": "result", "away": "result",
                         "1x": "double", "12": "double", "x2": "double",
                         "dc_1x": "double", "dc_12": "double", "dc_x2": "double"}


def bet_ban_label(group: str) -> str:
    return BET_BAN_GROUPS.get(group, (group, ()))[0]


def bet_ban_group(market_key: str | None = None, outcome: str | None = None) -> str | None:
    """Группа запрета для исхода: по market_key, а без него — по ключу исхода."""
    if market_key and market_key in _BAN_GROUP_BY_MARKET_KEY:
        return _BAN_GROUP_BY_MARKET_KEY[market_key]
    key = normalize_outcome_key(outcome or "")
    if not key:
        return None
    if key in _BAN_GROUP_BY_OUTCOME:
        return _BAN_GROUP_BY_OUTCOME[key]
    for prefix, group in _BAN_GROUP_BY_OUTCOME_PREFIX:
        if key.startswith(prefix):
            return group
    return None


def get_bet_bans(division_id: int | None = None, cursor=None) -> set[str]:
    """Группы, закрытые для матчей дивизиона: глобальные запреты ∪ запреты дивизиона.

    `division_id` None (общий кубок, матч без дивизиона) — только глобальные.
    Ошибку чтения не глушит: RiskEngine должен отказать, а не пропустить.
    """
    placeholders = ", ".join("?" * len(BET_BAN_KEYS))
    sql = (
        "SELECT DISTINCT limit_key FROM risk_limits_config"
        f" WHERE limit_key IN ({placeholders}) AND limit_value > 0"
        " AND ((scope_type = 'global' AND scope_id = 0)"
        " OR (scope_type = 'division' AND scope_id = ?))"
    )
    params = [*BET_BAN_KEYS, division_id if division_id else -1]
    if cursor is not None:
        cursor.execute(sql, params)
        rows = cursor.fetchall()
    else:
        with transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
    return {r["limit_key"][len(BET_BAN_PREFIX):] for r in rows}


def bet_ban_division(cursor, match_row) -> int | None:
    """Дивизион, чьи запреты действуют на матч.

    Лига — `matches.division_id`. Кубковый матч лежит на sentinel-дивизионе 0,
    его дивизион — владелец этапа (`cup_stages.division_id`); у общего кубка
    владельца нет, и действуют только глобальные запреты.
    """
    if match_is_cup(match_row):
        stage_id = _row_col(match_row, "stage_id")
        if not stage_id:
            return None
        cursor.execute("SELECT division_id FROM cup_stages WHERE id = ?", (stage_id,))
        row = cursor.fetchone()
        return int(row["division_id"]) if row and row["division_id"] else None
    division_id = _row_col(match_row, "division_id")
    return int(division_id) if division_id else None


def get_match_bet_bans(match_id: int) -> set[str]:
    """Закрытые группы для одного матча — для витрины Mini App."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT id, division_id, tournament_type, stage_id FROM matches WHERE id = ?",
            (match_id,),
        )
        row = cursor.fetchone()
        if not row:
            return set()
        return get_bet_bans(bet_ban_division(cursor, row), cursor=cursor)


def get_user_limit_overrides() -> list[dict]:
    """Игроки с личными лимитами — для списка в панели."""
    with transaction() as conn:
        rows = conn.execute("""
            SELECT rl.scope_id AS user_id, rl.limit_key, rl.limit_value,
                   u.username, u.team_name
            FROM risk_limits_config rl
            LEFT JOIN users u ON u.telegram_id = rl.scope_id
            WHERE rl.scope_type = 'user'
            ORDER BY rl.scope_id, rl.limit_key
        """).fetchall()
    players: dict[int, dict] = {}
    for r in rows:
        entry = players.setdefault(r["user_id"], {
            "user_id": r["user_id"], "username": r["username"],
            "team_name": r["team_name"], "limits": {},
        })
        entry["limits"][r["limit_key"]] = int(r["limit_value"])
    return list(players.values())


def delete_risk_limit_override(scope_type: str, scope_id: int, limit_key: str) -> bool:
    """Снять переопределение — уровень снова наследует лимит сверху."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM risk_limits_config WHERE scope_type = ? AND scope_id = ? AND limit_key = ?",
            (scope_type, scope_id, limit_key),
        )
        return cursor.rowcount > 0


def _fold_search_text(value) -> str:
    return str(value or "").casefold().replace("ё", "е")


def search_betting_players(query: str = "", banned_only: bool = False,
                           sort: str = "balance", limit: int = 50) -> list[dict]:
    """Игроки с кошельком или регистрацией: баланс, оборот, открытые купоны, запрет.

    Фильтр по имени — в Python: SQLite `LOWER()` не знает кириллицы, а
    названия клубов русские. Игроков — сотни, это дёшево.
    """
    with transaction() as conn:
        rows = conn.execute("""
            SELECT ids.user_id,
                   u.username,
                   u.team_name,
                   u.division_id,
                   d.name AS division_name,
                   w.balance,
                   w.total_wagered,
                   w.total_won,
                   w.bets_count,
                   w.bets_won,
                   COALESCE(ob.open_bets, 0) AS open_bets,
                   COALESCE(ob.open_stake, 0) AS open_stake,
                   b.reason AS ban_reason,
                   b.banned_at
            FROM (SELECT telegram_id AS user_id FROM users
                  UNION SELECT user_id FROM user_wallets) ids
            LEFT JOIN users u ON u.telegram_id = ids.user_id
            LEFT JOIN divisions d ON d.id = u.division_id
            LEFT JOIN user_wallets w ON w.user_id = ids.user_id
            LEFT JOIN betting_bans b ON b.user_id = ids.user_id
            LEFT JOIN (
                SELECT user_id, COUNT(*) AS open_bets, SUM(amount) AS open_stake
                FROM user_bets WHERE status = 'pending' GROUP BY user_id
            ) ob ON ob.user_id = ids.user_id
        """).fetchall()

    needle = _fold_search_text(query).strip().lstrip("@")
    players = []
    for row in rows:
        p = dict(row)
        p["is_banned"] = p["banned_at"] is not None
        if banned_only and not p["is_banned"]:
            continue
        if needle and not (
            needle == str(p["user_id"])
            or needle in _fold_search_text(p["username"])
            or needle in _fold_search_text(p["team_name"])
        ):
            continue
        players.append(p)

    sort_keys = {
        "balance": lambda p: -(p["balance"] or 0),
        "wagered": lambda p: -(p["total_wagered"] or 0),
        "open": lambda p: -(p["open_stake"] or 0),
        "name": lambda p: _fold_search_text(p["username"] or p["team_name"] or p["user_id"]),
    }
    players.sort(key=sort_keys.get(sort, sort_keys["balance"]))
    return players[:limit]


def get_betting_player(user_id: int) -> dict | None:
    """Карточка игрока для панели: профиль, кошелёк, итоги ставок, запрет, лимиты, движения монет."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT u.telegram_id, u.username, u.team_name, u.division_id,
                   d.name AS division_name, u.registered_at
            FROM users u
            LEFT JOIN divisions d ON d.id = u.division_id
            WHERE u.telegram_id = ?
        """, (user_id,))
        profile = cursor.fetchone()
        cursor.execute("SELECT * FROM user_wallets WHERE user_id = ?", (user_id,))
        wallet = cursor.fetchone()
        if not profile and not wallet:
            return None

    return {
        "user_id": user_id,
        "username": profile["username"] if profile else None,
        "team_name": profile["team_name"] if profile else None,
        "division_id": profile["division_id"] if profile else None,
        "division_name": profile["division_name"] if profile else None,
        "registered_at": profile["registered_at"] if profile else None,
        "wallet": dict(wallet) if wallet else None,
        "summary": get_user_bet_summary(user_id),
        "ban": get_betting_ban(user_id),
        "limit_overrides": get_risk_limit_overrides("user", user_id),
        "transactions": get_coin_transactions(user_id, limit=30),
    }


def _division_scope(division_ids: list[int] | None) -> tuple[str, list[int]]:
    """Кусок `IN (?, ?, …)` для дивизионов; None — без ограничения.

    Интерполируются только знаки вопроса, значения идут параметрами.
    Пустой список — «ничего не видно», а не «видно всё».
    """
    if division_ids is None:
        return "", []
    if not division_ids:
        return "(NULL)", []
    return "(" + ",".join("?" * len(division_ids)) + ")", [int(d) for d in division_ids]


def get_betting_dashboard(division_ids: list[int] | None = None) -> dict:
    """Сводка букмекера: открытый риск, оборот, GGR, рынки, алерты, крупнейшие купоны.

    GGR (валовый доход) считается только по рассчитанным купонам: ставки
    проигравших и выигравших минус выплаты. Возвраты дают ноль, открытые
    купоны — это риск, а не доход. Купон попадает в дивизион, если хотя бы
    одна его нога — матч этого дивизиона (как в `get_all_bets`).
    """
    in_list, div_params = _division_scope(division_ids)
    bet_scope = ""
    if in_list:
        bet_scope = f"""
            AND EXISTS (
                SELECT 1 FROM bet_items bi_d
                JOIN matches m_d ON m_d.id = bi_d.match_id
                WHERE bi_d.bet_id = ub.id AND COALESCE(m_d.division_id, 1) IN {in_list}
            )
        """

    now = now_msk()
    today_start = now.strftime("%Y-%m-%d 00:00:00")
    week_start = (now - datetime.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    chart_start = (now - datetime.timedelta(days=13)).strftime("%Y-%m-%d 00:00:00")

    with transaction() as conn:
        cursor = conn.cursor()

        cursor.execute(f"""
            SELECT
                COUNT(*) AS total_bets,
                COUNT(DISTINCT ub.user_id) AS bettors,
                COALESCE(SUM(CASE WHEN ub.status = 'pending' THEN 1 ELSE 0 END), 0) AS pending_count,
                COALESCE(SUM(CASE WHEN ub.status = 'pending' THEN ub.amount ELSE 0 END), 0) AS pending_stake,
                COALESCE(SUM(CASE WHEN ub.status = 'pending' THEN ub.potential_win ELSE 0 END), 0) AS pending_liability,
                COALESCE(SUM(CASE WHEN ub.status IN ('won', 'lost', 'cashed_out') THEN ub.amount ELSE 0 END), 0) AS settled_stake,
                COALESCE(SUM(CASE WHEN ub.status IN ('won', 'cashed_out') THEN ub.actual_payout ELSE 0 END), 0) AS paid_out,
                COALESCE(SUM(CASE WHEN ub.status = 'won' THEN 1 ELSE 0 END), 0) AS count_won,
                COALESCE(SUM(CASE WHEN ub.status = 'lost' THEN 1 ELSE 0 END), 0) AS count_lost,
                COALESCE(SUM(CASE WHEN ub.status IN ('refunded', 'cancelled') THEN 1 ELSE 0 END), 0) AS count_refunded,
                COALESCE(SUM(CASE WHEN ub.status = 'cashed_out' THEN 1 ELSE 0 END), 0) AS count_cashed_out
            FROM user_bets ub
            WHERE 1 = 1 {bet_scope}
        """, div_params)
        totals = dict(cursor.fetchone())
        totals["ggr"] = totals["settled_stake"] - totals["paid_out"]
        totals["margin_pct"] = (round(totals["ggr"] * 100.0 / totals["settled_stake"], 1)
                                if totals["settled_stake"] else None)

        periods = {}
        for name, start in (("today", today_start), ("week", week_start)):
            cursor.execute(f"""
                SELECT
                    COALESCE(SUM(CASE WHEN ub.created_at >= ? THEN 1 ELSE 0 END), 0) AS bets,
                    COUNT(DISTINCT CASE WHEN ub.created_at >= ? THEN ub.user_id END) AS bettors,
                    COALESCE(SUM(CASE WHEN ub.created_at >= ? AND ub.status NOT IN ('refunded', 'cancelled')
                                      THEN ub.amount ELSE 0 END), 0) AS turnover,
                    COALESCE(SUM(CASE WHEN ub.settled_at >= ? AND ub.status IN ('won', 'lost', 'cashed_out')
                                      THEN ub.amount ELSE 0 END), 0)
                  - COALESCE(SUM(CASE WHEN ub.settled_at >= ? AND ub.status IN ('won', 'cashed_out')
                                      THEN ub.actual_payout ELSE 0 END), 0) AS ggr
                FROM user_bets ub
                WHERE 1 = 1 {bet_scope}
            """, [start] * 5 + div_params)
            periods[name] = dict(cursor.fetchone())

        cursor.execute(f"""
            SELECT date(ub.created_at) AS day,
                   COUNT(*) AS bets,
                   COALESCE(SUM(CASE WHEN ub.status NOT IN ('refunded', 'cancelled') THEN ub.amount ELSE 0 END), 0) AS turnover
            FROM user_bets ub
            WHERE ub.created_at >= ? {bet_scope}
            GROUP BY date(ub.created_at)
            ORDER BY day
        """, [chart_start] + div_params)
        by_day = {r["day"]: dict(r) for r in cursor.fetchall()}
        daily = []
        for offset in range(13, -1, -1):
            day = (now - datetime.timedelta(days=offset)).strftime("%Y-%m-%d")
            daily.append(by_day.get(day, {"day": day, "bets": 0, "turnover": 0}))

        match_scope = f" AND COALESCE(m.division_id, 1) IN {in_list}" if in_list else ""
        cursor.execute(f"""
            SELECT mk.status, COUNT(*) AS cnt
            FROM markets mk
            JOIN matches m ON m.id = mk.match_id
            WHERE 1 = 1 {match_scope}
            GROUP BY mk.status
        """, div_params)
        markets = {s: 0 for s in ("open", "suspended", "closed", "settled", "voided")}
        for r in cursor.fetchall():
            markets[r["status"]] = r["cnt"]

        alert_scope = f" AND COALESCE(division_id, 1) IN {in_list}" if in_list else ""
        cursor.execute(f"""
            SELECT
                COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0) AS active,
                COALESCE(SUM(CASE WHEN status = 'active' AND severity IN ('high', 'critical') THEN 1 ELSE 0 END), 0) AS high
            FROM risk_alerts
            WHERE 1 = 1 {alert_scope}
        """, div_params)
        alerts = dict(cursor.fetchone())

        cursor.execute(f"""
            SELECT ub.id, ub.user_id, ub.bet_type, ub.amount, ub.total_odd, ub.potential_win, ub.created_at,
                   u.username, u.team_name AS user_team
            FROM user_bets ub
            LEFT JOIN users u ON u.telegram_id = ub.user_id
            WHERE ub.status = 'pending' {bet_scope}
            ORDER BY ub.potential_win DESC, ub.id DESC
            LIMIT 5
        """, div_params)
        top_liability = [dict(r) for r in cursor.fetchall()]

        cursor.execute(f"""
            SELECT ub.user_id, u.username, u.team_name AS user_team,
                   COUNT(*) AS bets,
                   COALESCE(SUM(CASE WHEN ub.status IN ('won', 'cashed_out') THEN ub.actual_payout ELSE 0 END), 0)
                 - COALESCE(SUM(ub.amount), 0) AS net_profit
            FROM user_bets ub
            LEFT JOIN users u ON u.telegram_id = ub.user_id
            WHERE ub.status IN ('won', 'lost', 'cashed_out') {bet_scope}
            GROUP BY ub.user_id
            ORDER BY net_profit DESC
            LIMIT 5
        """, div_params)
        top_winners = [dict(r) for r in cursor.fetchall()]

        economy = None
        if division_ids is None:
            cursor.execute("""
                SELECT COUNT(*) AS wallets, COALESCE(SUM(balance), 0) AS coins_in_wallets
                FROM user_wallets
            """)
            economy = dict(cursor.fetchone())
            cursor.execute("SELECT COUNT(*) AS cnt FROM betting_bans")
            economy["banned_players"] = cursor.fetchone()["cnt"]

        try:
            pause = _read_betting_pause(cursor)
        except (ValueError, TypeError, AttributeError):
            pause = {"global": {"reason": "Повреждённое состояние — приём ставок закрыт"}, "divisions": {}}

    return {
        "totals": totals,
        "periods": periods,
        "daily": daily,
        "markets": markets,
        "alerts": alerts,
        "top_liability": top_liability,
        "top_winners": top_winners,
        "economy": economy,
        "pause": pause,
    }


def get_betting_entity_divisions(entity: str, entity_id: int) -> set[int] | None:
    """Дивизионы рынка, исхода, купона или риск-алерта — для проверки прав админа дивизиона.

    None — сущность не найдена. Матч без дивизиона — это дивизион 1. У
    экспресса дивизионов может быть несколько: править его может только тот,
    кому доступны все.
    """
    queries = {
        "market": """
            SELECT COALESCE(m.division_id, 1) AS div
            FROM markets mk JOIN matches m ON m.id = mk.match_id
            WHERE mk.id = ?
        """,
        "selection": """
            SELECT COALESCE(m.division_id, 1) AS div
            FROM market_selections ms
            JOIN markets mk ON mk.id = ms.market_id
            JOIN matches m ON m.id = mk.match_id
            WHERE ms.id = ?
        """,
        "bet": """
            SELECT DISTINCT COALESCE(m.division_id, 1) AS div
            FROM user_bets ub
            LEFT JOIN bet_items bi ON bi.bet_id = ub.id
            LEFT JOIN matches m ON m.id = bi.match_id
            WHERE ub.id = ?
        """,
        "alert": """
            SELECT COALESCE(division_id, 1) AS div
            FROM risk_alerts
            WHERE id = ?
        """,
    }
    if entity not in queries:
        raise ValueError(f"Unknown betting entity: {entity}")
    with transaction() as conn:
        rows = conn.execute(queries[entity], (entity_id,)).fetchall()
    if not rows:
        return None
    return {r["div"] for r in rows}


_MARKET_BOARD_STATES = {
    "active": ("open", "suspended"),
    "closed": ("closed",),
    "finished": ("settled", "voided"),
}


def get_admin_market_board(division_ids: list[int] | None = None, state: str = "active",
                           query: str = "", limit: int = 20, offset: int = 0) -> tuple[list[dict], int]:
    """Матчи с рынками и исходами для экрана «Рынки»: статусы, коэффициенты, нагрузка.

    На каждом исходе — открытые купоны, в которых он стоит живой ногой: число,
    сумма ставок и потенциальная выплата. Для экспресса это купон целиком,
    поэтому суммы по исходам не складываются в общий риск — это индикатор
    того, куда идут деньги, а не бухгалтерия.
    """
    in_list, div_params = _division_scope(division_ids)
    statuses = _MARKET_BOARD_STATES.get(state)
    status_sql = ""
    status_params: list = []
    if statuses:
        status_sql = " AND mk.status IN (" + ",".join("?" * len(statuses)) + ")"
        status_params = list(statuses)
    match_scope = f" AND COALESCE(m.division_id, 1) IN {in_list}" if in_list else ""
    order = "ASC" if state == "active" else "DESC"

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT DISTINCT m.id AS match_id,
                   COALESCE(m.division_id, 1) AS division_id,
                   d.name AS division_name,
                   m.round_number,
                   m.tournament_type,
                   cs.stage AS cup_stage,
                   m.game_num_in_series,
                   m.cup_series_id,
                   COALESCE(m.is_series_header, 0) AS is_series_header,
                   COALESCE(m.player1_team, cs.team1_name, 'Хозяева') AS team1_name,
                   COALESCE(m.player2_team, cs.team2_name, 'Гости') AS team2_name,
                   m.status AS match_status,
                   m.match_date,
                   m.match_time,
                   m.live_minute,
                   m.player1_score,
                   m.player2_score
            FROM markets mk
            JOIN matches m ON m.id = mk.match_id
            LEFT JOIN divisions d ON d.id = m.division_id
            LEFT JOIN cup_series cs ON cs.id = m.cup_series_id
            WHERE 1 = 1 {status_sql} {match_scope}
            ORDER BY m.id {order}
        """, status_params + div_params)
        matches = [dict(r) for r in cursor.fetchall()]

        needle = _fold_search_text(query).strip()
        if needle:
            matches = [m for m in matches
                       if needle == str(m["match_id"])
                       or needle in _fold_search_text(m["team1_name"])
                       or needle in _fold_search_text(m["team2_name"])]
        total = len(matches)
        page = matches[offset:offset + limit]
        if not page:
            return [], total

        match_ids = [m["match_id"] for m in page]
        id_list = ",".join("?" * len(match_ids))
        cursor.execute(f"""
            SELECT mk.id, mk.match_id, mk.market_key, mk.market_name, mk.category, mk.status, mk.sort_order
            FROM markets mk
            WHERE mk.match_id IN ({id_list}) {status_sql}
            ORDER BY mk.match_id, mk.sort_order, mk.id
        """, match_ids + status_params)
        markets = [dict(r) for r in cursor.fetchall()]
        market_ids = [mk["id"] for mk in markets]

        selections_by_market: dict[int, list[dict]] = {}
        if market_ids:
            mk_list = ",".join("?" * len(market_ids))
            cursor.execute(f"""
                SELECT ms.id, ms.market_id, ms.selection_key, ms.selection_name, ms.odds_value,
                       ms.model_odds, ms.previous_odds, ms.odds_version, ms.status, ms.updated_at,
                       COALESCE(ex.bets, 0) AS open_bets,
                       COALESCE(ex.stake, 0) AS open_stake,
                       COALESCE(ex.liability, 0) AS open_liability
                FROM market_selections ms
                LEFT JOIN (
                    SELECT bi.selection_id,
                           COUNT(DISTINCT ub.id) AS bets,
                           SUM(ub.amount) AS stake,
                           SUM(ub.potential_win) AS liability
                    FROM bet_items bi
                    JOIN user_bets ub ON ub.id = bi.bet_id
                    WHERE ub.status = 'pending' AND bi.status = 'pending'
                      AND bi.market_id IN ({mk_list})
                    GROUP BY bi.selection_id
                ) ex ON ex.selection_id = ms.id
                WHERE ms.market_id IN ({mk_list})
                ORDER BY ms.market_id, ms.id
            """, market_ids + market_ids)
            for s in cursor.fetchall():
                selections_by_market.setdefault(s["market_id"], []).append(dict(s))

    markets_by_match: dict[int, list[dict]] = {}
    for mk in markets:
        mk["selections"] = selections_by_market.get(mk["id"], [])
        mk["open_bets"] = sum(s["open_bets"] for s in mk["selections"])
        markets_by_match.setdefault(mk["match_id"], []).append(mk)
    _attach_cup_labels(page)
    for m in page:
        m["markets"] = markets_by_match.get(m["match_id"], [])
    return page, total


# ═══ Logovo.bet: сверка «ИИ-прогноза» с сыгранными матчами ════════════════════

MIGRATION_026_AI_PICK_LOG = "026_ai_pick_log"


def _ensure_ai_pick_log(cursor: sqlite3.Cursor) -> None:
    """Миграция 026: журнал исходов, которые ИИ показал во вкладке «ИИ-прогноз».

    Одна строка на исход: повторный прогноз того же исхода (другой фильтр,
    пересчёт) перезаписывает её, так что в сверку идёт последняя оценка до
    матча. Рынок и исход записаны копией, а не внешним ключом: пересборка линии
    не должна стирать историю. Итог в таблице не хранится — его каждый раз
    считает `market_settler` по счёту матча, и исправленный счёт не требует
    пересчёта журнала.
    """
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_pick_log (
            selection_id INTEGER PRIMARY KEY,
            match_id INTEGER NOT NULL,
            division_id INTEGER,
            round_number INTEGER,
            team1 TEXT,
            team2 TEXT,
            market_key TEXT NOT NULL,
            market_group TEXT,
            market_name TEXT,
            selection_key TEXT NOT NULL,
            selection_name TEXT,
            odds REAL NOT NULL,
            probability REAL NOT NULL,
            line_probability REAL NOT NULL,
            model TEXT,
            predicted_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_ai_pick_log_match ON ai_pick_log(match_id)")
    cursor.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (MIGRATION_026_AI_PICK_LOG, "ai_pick_log: AI picks kept for the check against played matches"),
    )


_AI_LOG_PLAYED = ("confirmed", "completed")


def log_ai_picks(picks: list[dict], model: str | None) -> int:
    """Записать исходы, показанные ИИ. Исходы уже сыгранных матчей пропускаются:
    оценка, сделанная после результата, сверке не нужна."""
    rows = [p for p in picks if p.get("selection_id") and p.get("match_id")]
    if not rows:
        return 0
    match_ids = sorted({int(p["match_id"]) for p in rows})
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"SELECT id FROM matches WHERE id IN ({','.join('?' * len(match_ids))}) "
            f"AND status IN (?, ?)",
            match_ids + list(_AI_LOG_PLAYED),
        )
        played = {r["id"] for r in cursor.fetchall()}
        written = 0
        for p in rows:
            if int(p["match_id"]) in played:
                continue
            cursor.execute("""
                INSERT INTO ai_pick_log (
                    selection_id, match_id, division_id, round_number, team1, team2,
                    market_key, market_group, market_name, selection_key, selection_name,
                    odds, probability, line_probability, model, predicted_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
                ON CONFLICT(selection_id) DO UPDATE SET
                    odds = excluded.odds,
                    probability = excluded.probability,
                    line_probability = excluded.line_probability,
                    model = excluded.model,
                    predicted_at = excluded.predicted_at
            """, (
                int(p["selection_id"]), int(p["match_id"]), p.get("division_id"),
                p.get("round_number"), p.get("team1"), p.get("team2"),
                p.get("market_key") or "", p.get("market_group"), p.get("market_name"),
                p.get("selection_key") or "", p.get("selection_name"),
                float(p["odds"]), float(p["probability"]), float(p["line_probability"]), model,
            ))
            written += 1
        return written


def get_ai_pick_log(division_ids: list[int] | None = None) -> tuple[list[dict], int]:
    """Прогнозы ИИ по сыгранным матчам (со счётом и полями для расчёта) и
    число прогнозов, чьи матчи ещё не сыграны.

    Технические результаты и отменённые матчи в сверку не идут: счёт там
    назначен, а не сыгран. Удалённый матч выпадает сам — журнал без него
    ничего не значит.
    """
    in_list, div_params = _division_scope(division_ids)
    scope_sql = f" AND l.division_id IN {in_list}" if in_list else ""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"""
            SELECT l.*, m.player1_score, m.player2_score, m.ht_score1, m.ht_score2,
                   m.status AS match_status, m.tournament_type, m.cup_winner_team,
                   m.player1_team, m.player2_team, m.played_at, d.name AS division_name
            FROM ai_pick_log l
            JOIN matches m ON m.id = l.match_id
            LEFT JOIN divisions d ON d.id = l.division_id
            WHERE m.status IN (?, ?)
              AND m.player1_score IS NOT NULL AND m.player2_score IS NOT NULL
              AND COALESCE(m.is_technical, 0) = 0{scope_sql}
            ORDER BY COALESCE(m.played_at, l.predicted_at) DESC, l.selection_id DESC
        """, list(_AI_LOG_PLAYED) + div_params)
        played = [dict(r) for r in cursor.fetchall()]
        cursor.execute(f"""
            SELECT COUNT(*) FROM ai_pick_log l
            JOIN matches m ON m.id = l.match_id
            WHERE m.status NOT IN (?, ?, 'cancelled'){scope_sql}
        """, list(_AI_LOG_PLAYED) + div_params)
        pending = cursor.fetchone()[0]
    return _attach_cup_labels(played), pending


def get_tournament_standings(division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Alias for get_standings with division/season scoping."""
    return get_standings(division_id=division_id, season_id=season_id)


def get_tournament_results(limit: int = 30, division_id: int | None = None, season_id: int | None = None) -> list[dict]:
    """Retrieve finished matches archive filtered by division and season."""
    with transaction() as conn:
        cursor = conn.cursor()
        query = """
            SELECT m.*, 
                   COALESCE(m.player1_team, 'Хозяева') as team1_name,
                   COALESCE(m.player2_team, 'Гости') as team2_name
            FROM matches m
            WHERE m.status IN ('confirmed', 'completed', 'finished')
              AND m.player1_score IS NOT NULL AND m.player2_score IS NOT NULL
        """
        params = []
        if division_id is not None:
            query += " AND m.division_id = ?"
            params.append(division_id)
        if season_id is not None:
            query += " AND (m.season_id = ? OR m.season_id IS NULL)"
            params.append(season_id)
        query += " ORDER BY m.round_number DESC, m.id DESC LIMIT ?"
        params.append(limit)
        cursor.execute(query, params)
        return [dict(r) for r in cursor.fetchall()]


def settle_match_bets(match_id: int, score1: int, score2: int, match_status: str = "finished") -> list[dict]:
    """
    Settle all pending bets related to a finished match.
    Delegates to full-featured services.settlement_engine.

    `match_status="voided"` — расчёт технического результата: спортивных выплат
    нет, ординары возвращаются полностью, нога экспресса идёт по коэффициенту 1.00.
    """
    from services.settlement_engine import settle_match_predictions
    return settle_match_predictions(match_id, score1, score2, match_status=match_status)


def settle_all_pending_finished_matches() -> list[dict]:
    """
    Self-healing trigger: Scan all pending bet items for matches that are already
    completed/confirmed in the database and settle them immediately.

    Технические матчи (`is_technical = 1`) добираются здесь тем же правилом, что
    и в `set_technical_result`: со статусом `voided`, то есть без спортивных выплат.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT m.id, m.player1_score, m.player2_score,
                   COALESCE(m.is_technical, 0) AS is_technical
            FROM bet_items bi
            JOIN matches m ON bi.match_id = m.id
            WHERE bi.status = 'pending'
              AND m.status IN ('confirmed', 'completed')
              AND m.player1_score IS NOT NULL
              AND m.player2_score IS NOT NULL
        """)
        matches = cursor.fetchall()
        from services.settlement_engine import settle_match_predictions
        all_payouts = []
        for m in matches:
            status = "voided" if m["is_technical"] else "finished"
            res = settle_match_predictions(
                m["id"], m["player1_score"], m["player2_score"], match_status=status
            )
            all_payouts.extend(res)
        return all_payouts


# ═════════════════════════════════════════════════════════════════════════════
# 🎮 LOGOVO.BET — GAMIFICATION, PROGRESSION, QUESTS & SOCIAL REPOSITORY
# ═════════════════════════════════════════════════════════════════════════════

def seed_gamification_catalog(cursor) -> None:
    """
    Seed the achievements catalog (quests removed in v2.0).

    Награды откалиброваны под реальную экономику Logovo.bet, а не под круглые
    числа: стартовый кошелёк — `config.INITIAL_WALLET_BALANCE` (677 🪙), дейлик
    даёт 250 🪙, минимальная ставка — 10 🪙, потолок выплаты по купону — 10 000 🪙.
    Отсюда шкала по редкости: common ≈ один дейлик (150–300), rare ≈ 500–1 000,
    epic ≈ 1 000–1 500, legendary ≈ 2 500–5 000. XP держится примерно на половине
    монет, потому что уровень сам по себе доплачивает 500 🪙 за каждый левел
    (`add_user_xp`) — если считать XP щедро, монеты приходят дважды.

    `is_active = 1` проставляется здесь: снятые с вооружения достижения в этом
    списке просто отсутствуют, и миграция 016 гасит их флагом, не удаляя строку.
    """
    achievements = [
        ("ACH_FIRST_BET", "🐺 Первый шаг", "Сделать свой первый прогноз", "general", "common", 75, 150, "🎯"),
        ("ACH_FIRST_WIN", "🏆 Первая кровь", "Выиграть свой первый прогноз", "general", "common", 125, 250, "⚔️"),
        ("ACH_STREAK_3", "🔥 В ударе", "Оформить серию из 3 побед подряд", "streaks", "common", 150, 300, "🔥"),
        ("ACH_STREAK_5", "🎯 Снайпер", "Оформить серию из 5 побед подряд", "streaks", "rare", 300, 700, "🎯"),
        ("ACH_STREAK_10", "👑 Непобедимый", "Оформить серию из 10 побед подряд", "streaks", "legendary", 1200, 3000, "👑"),
        ("ACH_EXPRESS_3", "🚂 Экспресс-старт", "Собрать экспресс из 3+ событий", "parlays", "common", 100, 200, "🚂"),
        ("ACH_EXPRESS_ODD_5", "💥 Множитель x5", "Выиграть экспресс с коэффициентом 5.0+", "parlays", "rare", 250, 600, "💥"),
        ("ACH_EXPRESS_ODD_15", "🚀 Ракета x15", "Выиграть экспресс с коэффициентом 15.0+", "parlays", "epic", 600, 1500, "🚀"),
        ("ACH_EXPRESS_ODD_50", "🌌 Космос x50", "Выиграть экспресс с коэффициентом 50.0+", "parlays", "legendary", 1500, 4000, "🌌"),
        ("ACH_UNDERDOG", "🐺 Гроза Фаворитов", "Выиграть ординар с коэффициентом 3.5+", "odds", "rare", 200, 500, "⚡"),
        ("ACH_TOTAL_10_BETS", "📊 Любитель", "Сделать 10 любых прогнозов", "volume", "common", 100, 250, "📊"),
        ("ACH_TOTAL_50_BETS", "🏅 Регуляр", "Сделать 50 любых прогнозов", "volume", "rare", 300, 750, "🏅"),
        ("ACH_TOTAL_100_BETS", "💯 Центурион", "Сделать 100 любых прогнозов", "volume", "epic", 600, 1500, "💯"),
        ("ACH_COIN_MILLIONAIRE", "💰 Мешок Монет", "Накопить 25 000 🪙 на балансе", "wealth", "epic", 500, 1000, "💰"),
        ("ACH_COIN_TYCOON", "🏦 Олигарх Логова", "Накопить 100 000 🪙 на балансе", "wealth", "legendary", 1000, 2500, "🏦"),
        ("ACH_LOGIN_3", "📅 Разминка", "Заходить в игру 3 дня подряд", "loyalty", "common", 100, 250, "📅"),
        ("ACH_LOGIN_7", "🔥 Неделя в строю", "Заходить в игру 7 дней подряд", "loyalty", "rare", 250, 600, "🔥"),
        ("ACH_LOGIN_30", "🐺 Вожак Стаи", "Заходить в игру 30 дней подряд", "loyalty", "legendary", 1200, 3000, "🐺"),
        ("ACH_TB_SPECIALIST", "⚽ Голевой Маньяк", "Выиграть 5 прогнозов на Тотал Больше 2.5", "markets", "rare", 200, 500, "⚽"),
        ("ACH_BTTS_MASTER", "🤝 Обе Забьют", "Выиграть 5 прогнозов на Обе Забьют", "markets", "rare", 200, 500, "🤝"),
        # Phase 10 Competitive & Seasonal Achievements
        ("ACH_10_WINS", "🎯 10 Побед", "Выиграть 10 любых прогнозов", "volume", "common", 150, 300, "🎯"),
        ("ACH_50_WINS", "🏆 50 Побед", "Выиграть 50 любых прогнозов", "volume", "rare", 400, 1000, "🏆"),
        ("ACH_100_WINS", "👑 100 Побед", "Выиграть 100 любых прогнозов", "volume", "legendary", 1200, 3000, "👑"),
        ("ACH_POSITIVE_ROI", "📈 В Плюсе", "Достичь положительного ROI при 10+ прогнозах", "skill", "rare", 300, 700, "📈"),
        ("ACH_VALUE_HUNTER", "💎 Охотник за Валуем", "Выиграть 5 валуйных прогнозов с перевесом", "skill", "epic", 500, 1200, "💎"),
        ("ACH_NO_LOSS_STREAK", "🛡 Без Поражений", "Оформить серию из 7 побед подряд без поражений", "streaks", "epic", 600, 1500, "🛡"),
        ("ACH_SEASON_TOP_10", "🌟 Топ-10 Сезона", "Завершить сезон в топ-10 своего дивизиона", "seasonal", "epic", 600, 1500, "🌟"),
        ("ACH_SEASON_CHAMPION", "🥇 Чемпион Сезона", "Занять 1-е место в дивизионе по итогам сезона", "seasonal", "legendary", 2000, 5000, "🥇"),
        ("ACH_PROMOTED", "🚀 Повышение в Классе", "Заработать повышение в высший дивизион", "seasonal", "rare", 400, 1000, "🚀")
    ]
    for ach in achievements:
        cursor.execute("""
            INSERT INTO achievements_catalog (id, name, description, category, rarity, reward_xp, reward_coins, badge_icon, is_active)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                category = excluded.category,
                rarity = excluded.rarity,
                reward_xp = excluded.reward_xp,
                reward_coins = excluded.reward_coins,
                badge_icon = excluded.badge_icon,
                is_active = 1
        """, ach)

    # Phase 10: Seed default season rewards
    default_rewards = [
        ("REW_CHAMPION", None, None, "Чемпион Дивизиона", "coins", 10000, "BADGE_CHAMPION", "Чемпион Дивизиона 👑", "CHAMPION"),
        ("REW_TOP_3", None, None, "Призер Сезона (Топ-3)", "coins", 5000, "BADGE_TOP_3", "Призер Сезона 🥈", "TOP_3"),
        ("REW_TOP_10", None, None, "Элита Дивизиона (Топ-10)", "coins", 2500, "BADGE_TOP_10", "Топ-10 🌟", "TOP_10"),
        ("REW_PROMOTION", None, None, "Награда за Повышение", "coins", 3000, "BADGE_PROMOTED", "Повышен в классе 🚀", "PROMOTION"),
        ("REW_PARTICIPATION", None, None, "Участник Сезона", "xp", 500, None, None, "PARTICIPATION"),
    ]
    for rew in default_rewards:
        cursor.execute("""
            INSERT INTO season_rewards_catalog (id, season_id, division_id, name, reward_type, amount, badge_id, title, criteria)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                reward_type = excluded.reward_type,
                amount = excluded.amount,
                badge_id = excluded.badge_id,
                title = excluded.title,
                criteria = excluded.criteria
        """, rew)

    # Phase 10: Seed default division rules
    for div_id in range(1, 6):
        cursor.execute("""
            INSERT OR IGNORE INTO season_rules_config (season_id, division_id, promotion_slots, relegation_slots, min_bets_qualification, min_matches_qualification, created_at)
            VALUES (1, ?, 3, 3, 5, 3, datetime('now', '+3 hours'))
        """, (div_id,))


def get_or_create_progression(user_id: int) -> dict:
    """Fetch user's XP, level, streak and cosmetic profile or create a fresh one."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM user_progression WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        cursor.execute("""
            INSERT INTO user_progression (user_id, level, current_xp, total_xp_earned, current_streak, best_streak, login_streak, best_login_streak, streak_shields, equipped_frame, equipped_title, updated_at)
            VALUES (?, 1, 0, 0, 0, 0, 0, 0, 1, 'default', 'Новичок', datetime('now', '+3 hours'))
        """, (user_id,))
        cursor.execute("SELECT * FROM user_progression WHERE user_id = ?", (user_id,))
        return dict(cursor.fetchone())


def add_user_xp(user_id: int, xp_amount: int) -> dict:
    """
    Safely credit XP to user, calculate level ups, and award coin milestones.
    Returns {level, current_xp, total_xp, leveled_up, reward_coins, new_title}.
    """
    if xp_amount <= 0:
        p = get_or_create_progression(user_id)
        return {"level": p["level"], "current_xp": p["current_xp"], "total_xp": p["total_xp_earned"], "leveled_up": False, "reward_coins": 0}

    import math
    with transaction() as conn:
        cursor = conn.cursor()
        get_or_create_wallet(user_id)
        p = get_or_create_progression(user_id)
        cur_level = p["level"]
        new_total_xp = p["total_xp_earned"] + xp_amount
        
        # Level formula: Level = 1 + floor(sqrt(total_xp / 150))
        calculated_level = max(1, 1 + int(math.sqrt(new_total_xp / 150)))
        
        leveled_up = calculated_level > cur_level
        reward_coins = 0
        
        title = p["equipped_title"]
        if calculated_level >= 50:
            title = "Легенда Логова 👑"
        elif calculated_level >= 35:
            title = "Элитный Аналитик ⚡"
        elif calculated_level >= 20:
            title = "Мастер Экспрессов 🚂"
        elif calculated_level >= 10:
            title = "Опытный Каппер 🎯"
        elif calculated_level >= 5:
            title = "Тактик 🐾"

        if leveled_up:
            reward_coins = (calculated_level - cur_level) * 500
            cursor.execute("""
                UPDATE user_wallets 
                SET balance = balance + ?, updated_at = datetime('now', '+3 hours')
                WHERE user_id = ?
            """, (reward_coins, user_id))
            cursor.execute("""
                INSERT INTO coin_transactions (user_id, amount, transaction_type, reference_id, balance_after, created_at)
                VALUES (?, ?, 'level_up_reward', ?, (SELECT balance FROM user_wallets WHERE user_id = ?), datetime('now', '+3 hours'))
            """, (user_id, reward_coins, calculated_level, user_id))

        # XP required for next level
        xp_for_current_lvl = int(((calculated_level - 1) ** 2) * 150)
        xp_for_next_lvl = int((calculated_level ** 2) * 150)
        lvl_progress_xp = new_total_xp - xp_for_current_lvl

        cursor.execute("""
            UPDATE user_progression
            SET level = ?, current_xp = ?, total_xp_earned = ?, equipped_title = ?, updated_at = datetime('now', '+3 hours')
            WHERE user_id = ?
        """, (calculated_level, lvl_progress_xp, new_total_xp, title, user_id))

        return {
            "level": calculated_level,
            "current_xp": lvl_progress_xp,
            "next_level_xp": xp_for_next_lvl - xp_for_current_lvl,
            "total_xp": new_total_xp,
            "leveled_up": leveled_up,
            "reward_coins": reward_coins,
            "title": title
        }


def check_and_update_login_streak(user_id: int) -> dict:
    """
    Evaluate the consecutive-login-days streak for user.
    Handles streak increment, resets, and streak shield protection.

    Владеет колонками `login_streak` / `best_login_streak`. Серия побед по
    ставкам живёт в `current_streak` и принадлежит StreakEngine — счётчики
    разные и пересекаться не должны.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        p = get_or_create_progression(user_id)
        today_str = today_msk().isoformat()
        last_active = p.get("last_active_date")

        if last_active == today_str:
            return {
                "streak": p["login_streak"],
                "best_streak": p["best_login_streak"],
                "shield_used": False,
                "streak_shield_count": p["streak_shields"]
            }

        cur_streak = p["login_streak"]
        shield_used = False
        shields = p["streak_shields"]

        if last_active:
            try:
                last_dt = datetime.date.fromisoformat(last_active)
                delta_days = (today_msk() - last_dt).days
                if delta_days == 1:
                    cur_streak += 1
                elif delta_days == 2 and shields > 0:
                    # Shield consumed to save streak
                    shields -= 1
                    shield_used = True
                    cur_streak += 1
                else:
                    cur_streak = 1
            except Exception:
                cur_streak = 1
        else:
            cur_streak = 1

        best = max(cur_streak, p["best_login_streak"])
        cursor.execute("""
            UPDATE user_progression
            SET login_streak = ?, best_login_streak = ?, last_active_date = ?, streak_shields = ?, updated_at = datetime('now', '+3 hours')
            WHERE user_id = ?
        """, (cur_streak, best, today_str, shields, user_id))

        # Trigger login achievements. Порог читается из login_streak, а не из
        # current_streak: последний принадлежит StreakEngine и считает победы.
        if cur_streak >= 3:
            unlock_achievement(user_id, "ACH_LOGIN_3")
        if cur_streak >= 7:
            unlock_achievement(user_id, "ACH_LOGIN_7")
        if cur_streak >= 30:
            unlock_achievement(user_id, "ACH_LOGIN_30")

        return {
            "streak": cur_streak,
            "best_streak": best,
            "shield_used": shield_used,
            "streak_shield_count": shields
        }




def unlock_achievement(user_id: int, achievement_id: str) -> bool:
    """Unlock an achievement for user if not already unlocked."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM user_achievements WHERE user_id = ? AND achievement_id = ?", (user_id, achievement_id))
        if cursor.fetchone():
            return False

        cursor.execute("""
            INSERT OR IGNORE INTO user_achievements (user_id, achievement_id, is_claimed, unlocked_at)
            VALUES (?, ?, 0, datetime('now', '+3 hours'))
        """, (user_id, achievement_id))
        return True


def get_user_achievements(user_id: int) -> list[dict]:
    """
    Return list of all catalog achievements with user unlocked status.

    Снятые с каталога достижения (`is_active = 0`) уходят из выдачи, чтобы не
    висеть в знаменателе «получено N из M» недостижимым балластом, но остаются
    видны тому, кто успел их открыть — иначе из профиля пропала бы уже
    полученная награда.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ac.*, 
                   CASE WHEN ua.id IS NOT NULL THEN 1 ELSE 0 END as is_unlocked,
                   COALESCE(ua.is_claimed, 0) as is_claimed,
                   ua.unlocked_at
            FROM achievements_catalog ac
            LEFT JOIN user_achievements ua ON ac.id = ua.achievement_id AND ua.user_id = ?
            WHERE ac.is_active = 1 OR ua.id IS NOT NULL
            ORDER BY is_unlocked DESC, ac.reward_xp DESC
        """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]


def claim_achievement_reward(user_id: int, achievement_id: str) -> tuple[bool, str, dict]:
    """Claim reward for an unlocked achievement."""
    from config import is_global_lockdown_enabled
    if is_global_lockdown_enabled():
        from handlers.base import is_global_admin
        if not is_global_admin(user_id):
            return False, "Logovo.bet временно закрыт для пользователей.", {}

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT ua.id, ua.is_claimed, ac.reward_xp, ac.reward_coins, ac.name
            FROM user_achievements ua
            JOIN achievements_catalog ac ON ua.achievement_id = ac.id
            WHERE ua.user_id = ? AND ua.achievement_id = ?
        """, (user_id, achievement_id))
        row = cursor.fetchone()

        if not row:
            return False, "Достижение ещё не разблокировано.", {}
        cursor.execute("""
            UPDATE user_achievements
            SET is_claimed = 1
            WHERE user_id = ? AND achievement_id = ? AND is_claimed = 0
        """, (user_id, achievement_id))
        if cursor.rowcount == 0:
            return False, "Награда за достижение уже получена.", {}

        xp_res = add_user_xp(user_id, row["reward_xp"])
        add_coins(user_id, row["reward_coins"], tx_type="achievement_reward")

        return True, f"🏆 Достижение получено: +{row['reward_coins']} 🪙 и +{row['reward_xp']} XP!", {
            "coins": row["reward_coins"],
            "xp": row["reward_xp"],
            "progression": xp_res
        }


def evaluate_betting_achievements(user_id: int, bet_payload: dict | None = None) -> None:
    """Scan and trigger achievements on bet placement or win."""
    with transaction() as conn:
        cursor = conn.cursor()
        wallet = get_or_create_wallet(user_id)
        prog = get_or_create_progression(user_id)

        # Volume
        cnt = wallet["bets_count"]
        if cnt >= 1:
            unlock_achievement(user_id, "ACH_FIRST_BET")
        if cnt >= 10:
            unlock_achievement(user_id, "ACH_TOTAL_10_BETS")
        if cnt >= 50:
            unlock_achievement(user_id, "ACH_TOTAL_50_BETS")
        if cnt >= 100:
            unlock_achievement(user_id, "ACH_TOTAL_100_BETS")

        # Wealth
        bal = wallet["balance"]
        if bal >= 25000:
            unlock_achievement(user_id, "ACH_COIN_MILLIONAIRE")
        if bal >= 100000:
            unlock_achievement(user_id, "ACH_COIN_TYCOON")

        # Wins & Streaks
        won_cnt = wallet["bets_won"]
        if won_cnt >= 1:
            unlock_achievement(user_id, "ACH_FIRST_WIN")

        # Parlay / Odd achievements from payload
        if bet_payload:
            b_type = bet_payload.get("bet_type")
            odd = float(bet_payload.get("total_odd", 1.0))
            if b_type == "express":
                unlock_achievement(user_id, "ACH_EXPRESS_3")
                if odd >= 5.0:
                    unlock_achievement(user_id, "ACH_EXPRESS_ODD_5")
                if odd >= 15.0:
                    unlock_achievement(user_id, "ACH_EXPRESS_ODD_15")
                if odd >= 50.0:
                    unlock_achievement(user_id, "ACH_EXPRESS_ODD_50")
            elif b_type == "single" and odd >= 3.5:
                unlock_achievement(user_id, "ACH_UNDERDOG")




def get_public_gamer_profile(user_id: int) -> dict:
    """Assemble public esports gamer card with radar stats, badges and achievements."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
        user_row = cursor.fetchone()
        
        wallet = get_or_create_wallet(user_id)
        prog = get_or_create_progression(user_id)
        achievements = get_user_achievements(user_id)
        unlocked_ach = [a for a in achievements if a["is_unlocked"]]

        win_rate = round((wallet["bets_won"] / max(1, wallet["bets_count"])) * 100, 1)

        return {
            "user_id": user_id,
            "username": user_row["username"] if user_row else f"Каппер #{user_id}",
            "team_name": user_row["team_name"] if user_row else "Свободный игрок",
            "level": prog["level"],
            "current_xp": prog["current_xp"],
            "total_xp": prog["total_xp_earned"],
            "title": prog["equipped_title"],
            "frame": prog["equipped_frame"],
            "streak": prog["current_streak"],
            "best_streak": prog["best_streak"],
            "bets_count": wallet["bets_count"],
            "bets_won": wallet["bets_won"],
            "win_rate": win_rate,
            "unlocked_achievements_count": len(unlocked_ach),
            "achievements": unlocked_ach[:6]
        }


# ─── Phase 10: Competitive Profile, Season Stats & Career History ───────────

def get_user_favorite_stats(user_id: int) -> dict:
    """Analyze settled user bets to determine favorite markets, teams, prediction accuracy, and value hit rate."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT bi.market_id, bi.selection_id, bi.status, bi.odd,
                   m.market_key, m.match_id,
                   mat.player1_team, mat.player2_team
            FROM bet_items bi
            JOIN user_bets ub ON bi.bet_id = ub.id
            LEFT JOIN markets m ON bi.market_id = m.id
            LEFT JOIN matches mat ON bi.match_id = mat.id
            WHERE ub.user_id = ? AND bi.status IN ('won', 'lost')
        """, (user_id,))
        items = cursor.fetchall()

        if not items:
            return {
                "favorite_markets": [],
                "favorite_teams": [],
                "prediction_accuracy": 0.0,
                "value_hit_rate": 0.0
            }

        market_counts = {}
        team_counts = {}
        won_count = 0
        value_bets_count = 0
        value_won_count = 0

        for r in items:
            m_key = r["market_key"] or "1x2"
            market_counts[m_key] = market_counts.get(m_key, 0) + 1

            t1 = r["player1_team"]
            t2 = r["player2_team"]
            if t1:
                team_counts[t1] = team_counts.get(t1, 0) + 1
            if t2:
                team_counts[t2] = team_counts.get(t2, 0) + 1

            is_win = (r["status"] == "won")
            if is_win:
                won_count += 1

            odd = float(r["odd"] or 1.0)
            if odd >= 2.0:
                value_bets_count += 1
                if is_win:
                    value_won_count += 1

        sorted_markets = sorted(market_counts.keys(), key=lambda k: market_counts[k], reverse=True)[:3]
        sorted_teams = sorted(team_counts.keys(), key=lambda k: team_counts[k], reverse=True)[:3]
        accuracy = round((won_count / len(items)) * 100, 1)
        val_hit_rate = round((value_won_count / max(1, value_bets_count)) * 100, 1) if value_bets_count > 0 else 0.0

        return {
            "favorite_markets": sorted_markets,
            "favorite_teams": sorted_teams,
            "prediction_accuracy": accuracy,
            "value_hit_rate": val_hit_rate
        }


def get_or_create_season_stats(user_id: int, season_id: int | None = None, division_id: int | None = None) -> dict:
    """Fetch or initialize player's season-scoped stats record."""
    with transaction() as conn:
        cursor = conn.cursor()

        target_s_id = season_id
        if target_s_id is None:
            act = get_active_season()
            target_s_id = act["id"] if act else 1

        target_d_id = division_id
        if target_d_id is None:
            cursor.execute("SELECT division_id FROM users WHERE telegram_id = ?", (user_id,))
            u_row = cursor.fetchone()
            target_d_id = (u_row["division_id"] if u_row and u_row["division_id"] else 1)

        cursor.execute("""
            SELECT * FROM season_player_stats
            WHERE user_id = ? AND season_id = ? AND division_id = ?
        """, (user_id, target_s_id, target_d_id))
        row = cursor.fetchone()
        if row:
            return dict(row)

        cursor.execute("""
            INSERT OR IGNORE INTO season_player_stats (user_id, season_id, division_id, rating, confidence, season_points, status, updated_at)
            VALUES (?, ?, ?, 1200.0, 350.0, 0.0, 'QUALIFYING', datetime('now', '+3 hours'))
        """, (user_id, target_s_id, target_d_id))

        cursor.execute("""
            SELECT * FROM season_player_stats
            WHERE user_id = ? AND season_id = ? AND division_id = ?
        """, (user_id, target_s_id, target_d_id))
        return dict(cursor.fetchone())


def update_season_player_stats(user_id: int, season_id: int, division_id: int, **kwargs) -> None:
    """Safely update dynamic season player stats fields."""
    if not kwargs:
        return
    with transaction() as conn:
        cursor = conn.cursor()
        # Ensure row exists
        get_or_create_season_stats(user_id, season_id, division_id)

        set_clauses = []
        params = []
        for k, v in kwargs.items():
            set_clauses.append(f"{k} = ?")
            params.append(v)
        set_clauses.append("updated_at = datetime('now', '+3 hours')")

        params.extend([user_id, season_id, division_id])
        sql = f"UPDATE season_player_stats SET {', '.join(set_clauses)} WHERE user_id = ? AND season_id = ? AND division_id = ?"
        cursor.execute(sql, tuple(params))


def get_player_season_stats(user_id: int, season_id: int | None = None, division_id: int | None = None) -> dict:
    """Retrieve player season stats."""
    return get_or_create_season_stats(user_id, season_id, division_id)


def get_player_career_stats(user_id: int) -> dict:
    """Aggregate career-wide metrics across all completed and active seasons."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT
                COUNT(id) as total_bets,
                SUM(CASE WHEN status = 'won' THEN 1 ELSE 0 END) as career_wins,
                SUM(CASE WHEN status = 'lost' THEN 1 ELSE 0 END) as career_losses,
                SUM(CASE WHEN status = 'cashed_out' THEN 1 ELSE 0 END) as career_cashouts,
                SUM(amount) as career_stake,
                SUM(CASE WHEN status IN ('won', 'cashed_out') THEN actual_payout ELSE 0 END) as career_payout
            FROM user_bets
            WHERE user_id = ? AND status IN ('won', 'lost', 'cashed_out')
        """, (user_id,))
        b_row = cursor.fetchone()

        total_bets = b_row["total_bets"] or 0
        career_wins = b_row["career_wins"] or 0
        career_losses = b_row["career_losses"] or 0
        career_cashouts = b_row["career_cashouts"] or 0
        career_stake = b_row["career_stake"] or 0
        career_payout = b_row["career_payout"] or 0

        career_roi = round(((career_payout - career_stake) / max(1, career_stake)) * 100, 1) if career_stake > 0 else 0.0
        career_acc = round((career_wins / max(1, total_bets)) * 100, 1) if total_bets > 0 else 0.0

        cursor.execute("SELECT COUNT(DISTINCT season_id) as seasons_played FROM season_player_stats WHERE user_id = ?", (user_id,))
        s_played = cursor.fetchone()["seasons_played"] or 0

        cursor.execute("""
            SELECT
                SUM(CASE WHEN final_rank = 1 THEN 1 ELSE 0 END) as seasons_won,
                SUM(CASE WHEN promotion_status = 'PROMOTED' THEN 1 ELSE 0 END) as promotions,
                SUM(CASE WHEN promotion_status = 'RELEGATED' THEN 1 ELSE 0 END) as relegations,
                MIN(final_rank) as best_finish,
                MAX(best_streak) as longest_streak
            FROM season_snapshots
            WHERE user_id = ?
        """, (user_id,))
        snap_row = cursor.fetchone()

        prog = get_or_create_progression(user_id)
        ach = get_user_achievements(user_id)

        longest = max(prog.get("best_streak", 0), snap_row["longest_streak"] or 0)

        return {
            "user_id": user_id,
            "career_bets": total_bets,
            "career_wins": career_wins,
            "career_losses": career_losses,
            "career_cashouts": career_cashouts,
            "career_stake": career_stake,
            "career_payout": career_payout,
            "career_roi": career_roi,
            "career_accuracy": career_acc,
            "seasons_played": max(1, s_played),
            "seasons_won": snap_row["seasons_won"] or 0,
            "promotions": snap_row["promotions"] or 0,
            "relegations": snap_row["relegations"] or 0,
            "achievements_count": sum(1 for a in ach if a["is_unlocked"]),
            "best_finish": snap_row["best_finish"] or 1,
            "longest_streak": longest
        }


def get_public_player_profile(user_id: int, season_id: int | None = None, division_id: int | None = None) -> dict:
    """
    Assemble strictly PUBLIC player profile 2.0.
    CRITICAL: Never reveals wallet balance, coin totals, or private betting transactions.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (user_id,))
        user_row = cursor.fetchone()

        prog = get_or_create_progression(user_id)
        s_stats = get_or_create_season_stats(user_id, season_id, division_id)
        fav = get_user_favorite_stats(user_id)
        achievements = get_user_achievements(user_id)
        unlocked_ach = [a for a in achievements if a["is_unlocked"]]

        # Calculate rank dynamically if not frozen
        cursor.execute("""
            SELECT COUNT(*) + 1 as rank
            FROM season_player_stats
            WHERE season_id = ? AND division_id = ? AND (rating > ? OR (rating = ? AND season_points > ?))
        """, (s_stats["season_id"], s_stats["division_id"], s_stats["rating"], s_stats["rating"], s_stats["season_points"]))
        curr_rank = cursor.fetchone()["rank"]

        return {
            "user_id": user_id,
            "username": user_row["username"] if user_row and user_row["username"] else f"Каппер #{user_id}",
            "team_name": user_row["team_name"] if user_row and user_row["team_name"] else "Свободный игрок",
            "division_id": s_stats["division_id"],
            "season_id": s_stats["season_id"],
            "rating": round(float(s_stats["rating"]), 1),
            "rank": curr_rank,
            "level": prog["level"],
            "current_xp": prog["current_xp"],
            "total_xp": prog["total_xp_earned"],
            "experience": prog["total_xp_earned"],
            "tier": ("MASTER" if s_stats["rating"] >= 1600 else "ELITE" if s_stats["rating"] >= 1450 else "PRO" if s_stats["rating"] >= 1300 else "RISING" if s_stats["rating"] >= 1150 else "ROOKIE"),
            "title": prog["equipped_title"],
            "frame": prog["equipped_frame"],
            "season_points": round(float(s_stats["season_points"]), 1),
            "total_bets": s_stats["total_bets"],
            "settled_bets": s_stats["settled_bets"],
            "wins": s_stats["wins"],
            "losses": s_stats["losses"],
            "win_rate": round(float(s_stats["win_rate"]), 1),
            "roi": round(float(s_stats["roi"]), 1),
            "best_streak": s_stats["best_streak"],
            "current_streak": s_stats["current_streak"],
            "favorite_markets": fav["favorite_markets"],
            "favorite_teams": fav["favorite_teams"],
            "prediction_accuracy": fav["prediction_accuracy"],
            "value_hit_rate": fav["value_hit_rate"],
            "status": s_stats["status"],
            "unlocked_achievements_count": len(unlocked_ach),
            "achievements": unlocked_ach[:6]
        }


def get_private_player_profile(user_id: int, season_id: int | None = None, division_id: int | None = None) -> dict:
    """
    Assemble PRIVATE player profile for authenticated user only.
    Contains wallet, stake volume, and financial metrics.
    """
    pub = get_public_player_profile(user_id, season_id, division_id)
    with transaction() as conn:
        cursor = conn.cursor()
        wallet = get_or_create_wallet(user_id)
        s_stats = get_player_season_stats(user_id, season_id, division_id)
        career = get_player_career_stats(user_id)

        pub.update({
            "balance": wallet["balance"],
            "wallet": wallet,
            "total_stake": s_stats["total_stake"],
            "total_payout": s_stats["total_payout"],
            "career": career
        })
        return pub


def create_season_snapshot(
    season_id: int,
    division_id: int,
    user_id: int,
    final_rank: int,
    final_rating: float,
    season_points: float,
    wins: int,
    losses: int,
    voids: int,
    settled_bets: int,
    win_rate: float,
    roi: float,
    total_stake: int,
    total_payout: int,
    best_streak: int,
    promotion_status: str,
    rewards_json: str | None = None
) -> int:
    """Create an immutable historical snapshot for a player upon season completion."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO season_snapshots (
                season_id, division_id, user_id, final_rank, final_rating, season_points,
                wins, losses, voids, settled_bets, win_rate, roi, total_stake, total_payout,
                best_streak, promotion_status, rewards_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(season_id, division_id, user_id) DO NOTHING
        """, (
            season_id, division_id, user_id, final_rank, final_rating, season_points,
            wins, losses, voids, settled_bets, win_rate, roi, total_stake, total_payout,
            best_streak, promotion_status, rewards_json
        ))
        return cursor.lastrowid or 0


def get_season_snapshots(season_id: int, division_id: int | None = None) -> list[dict]:
    """Retrieve immutable snapshots for a finished season."""
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is not None:
            cursor.execute("""
                SELECT ss.*, u.username, u.team_name
                FROM season_snapshots ss
                LEFT JOIN users u ON ss.user_id = u.telegram_id
                WHERE ss.season_id = ? AND ss.division_id = ?
                ORDER BY ss.final_rank ASC
            """, (season_id, division_id))
        else:
            cursor.execute("""
                SELECT ss.*, u.username, u.team_name
                FROM season_snapshots ss
                LEFT JOIN users u ON ss.user_id = u.telegram_id
                WHERE ss.season_id = ?
                ORDER BY ss.division_id ASC, ss.final_rank ASC
            """, (season_id,))
        return [dict(r) for r in cursor.fetchall()]


def get_season_rules(season_id: int, division_id: int) -> dict:
    """Retrieve promotion/relegation and qualification rules for division in season."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM season_rules_config
            WHERE season_id = ? AND division_id = ?
        """, (season_id, division_id))
        row = cursor.fetchone()
        if row:
            return dict(row)
        # Default fallback
        return {
            "season_id": season_id,
            "division_id": division_id,
            "promotion_slots": 3,
            "relegation_slots": 3,
            "min_bets_qualification": 5,
            "min_matches_qualification": 3
        }


def set_season_rules(
    season_id: int,
    division_id: int,
    promotion_slots: int = 3,
    relegation_slots: int = 3,
    min_bets_qualification: int = 5,
    min_matches_qualification: int = 3
) -> None:
    """Configure or update promotion, relegation, and qualification thresholds."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO season_rules_config (
                season_id, division_id, promotion_slots, relegation_slots,
                min_bets_qualification, min_matches_qualification, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(season_id, division_id) DO UPDATE SET
                promotion_slots = excluded.promotion_slots,
                relegation_slots = excluded.relegation_slots,
                min_bets_qualification = excluded.min_bets_qualification,
                min_matches_qualification = excluded.min_matches_qualification
        """, (season_id, division_id, promotion_slots, relegation_slots, min_bets_qualification, min_matches_qualification))


def record_season_reward_in_ledger(
    season_id: int,
    division_id: int,
    user_id: int,
    reward_id: str,
    reward_type: str,
    coins_awarded: int = 0,
    xp_awarded: int = 0,
    badge_awarded: str | None = None
) -> bool:
    """
    Idempotently record a season reward.
    Returns True if newly recorded, False if already recorded.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO season_reward_ledger (
                season_id, division_id, user_id, reward_id, reward_type,
                coins_awarded, xp_awarded, badge_awarded, status, distributed_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'DISTRIBUTED', datetime('now', '+3 hours'), datetime('now', '+3 hours'))
            ON CONFLICT(user_id, season_id, reward_id) DO NOTHING
        """, (season_id, division_id, user_id, reward_id, reward_type, coins_awarded, xp_awarded, badge_awarded))
        return cursor.rowcount > 0


def get_user_season_rewards(user_id: int, season_id: int | None = None) -> list[dict]:
    """List claimed or distributed season rewards for a user."""
    with transaction() as conn:
        cursor = conn.cursor()
        if season_id is not None:
            cursor.execute("""
                SELECT * FROM season_reward_ledger
                WHERE user_id = ? AND season_id = ?
                ORDER BY created_at DESC
            """, (user_id, season_id))
        else:
            cursor.execute("""
                SELECT * FROM season_reward_ledger
                WHERE user_id = ?
                ORDER BY created_at DESC
            """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]


# ─── LOGOVO: Division Management & Multi-Topic Routing ──────────────────────

def create_division(
    name: str,
    code: str,
    tournament_id: int = 1,
    topic_id: int | None = None,
    sort_order: int = 0
) -> int:
    """Create a new division within a tournament."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO divisions (name, code, tournament_id, topic_id, sort_order, created_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (name.strip(), code.strip().upper(), tournament_id, topic_id, sort_order))
        return cursor.lastrowid


def ensure_canonical_divisions() -> None:
    """Ensure the 5 canonical divisions exist in the database."""
    with transaction() as conn:
        conn.cursor().execute("""
            INSERT OR IGNORE INTO divisions (id, tournament_id, name, code, season_id, sort_order, created_at)
            VALUES
                (1, 1, 'Дивизион 1', 'DIV_1', 1, 1, datetime('now', '+3 hours')),
                (2, 1, 'Дивизион 2', 'DIV_2', 1, 2, datetime('now', '+3 hours')),
                (3, 1, 'Дивизион 3', 'DIV_3', 1, 3, datetime('now', '+3 hours')),
                (4, 1, 'Дивизион 4', 'DIV_4', 1, 4, datetime('now', '+3 hours')),
                (5, 1, 'Дивизион 5', 'DIV_5', 1, 5, datetime('now', '+3 hours'))
        """)


def repair_canonical_division_codes() -> list[tuple[int, str, str]]:
    """Вернуть дивизионам 1–5 канонические коды DIV_1…DIV_5.

    `ensure_canonical_divisions()` объявляет эти коды, но вставляет их через
    INSERT OR IGNORE — строку, созданную раньше неё, она не трогает. А код при
    ручном создании собирался только из латиницы в названии: у «Дивизион 1»
    латиницы нет, и код выходил случайным (`DIV_DCC7`). По коду ищется сезонный
    состав `config.DIVISION_CLUBS`, поэтому случайный код означает дивизион без
    единого клуба — плюс промах мимо палитры `graphics.division_theme.THEMES`.

    Чиним только сломанное: строку трогаем, если её текущий код не находит
    состав, канонический находит, и его не занял другой дивизион. На здоровой
    базе не меняет ничего, так что вызывать можно повторно.

    Возвращает список `(division_id, старый код, новый код)`.
    """
    from config import DIVISION_CLUBS

    repaired: list[tuple[int, str, str]] = []
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, code FROM divisions")
        codes = {int(r["id"]): (r["code"] or "").strip().upper() for r in cursor.fetchall()}

        for div_id in range(1, 6):
            if div_id not in codes:
                continue
            current, target = codes[div_id], f"DIV_{div_id}"
            if current == target or DIVISION_CLUBS.get(current):
                continue
            if not DIVISION_CLUBS.get(target):
                continue
            if any(code == target for other, code in codes.items() if other != div_id):
                continue
            cursor.execute("UPDATE divisions SET code = ? WHERE id = ?", (target, div_id))
            codes[div_id] = target
            repaired.append((div_id, current, target))

    if repaired:
        logger.info(f"Canonical division codes repaired: {repaired}")
    return repaired


def get_divisions(is_active: bool | None = None, only_active: bool | None = None) -> list[dict]:
    """Retrieve all divisions, optionally filtered by is_active status."""
    ensure_canonical_divisions()
    if only_active is not None and is_active is None:
        is_active = only_active
    with transaction() as conn:
        cursor = conn.cursor()
        if is_active is None:
            cursor.execute("SELECT * FROM divisions ORDER BY sort_order ASC, id ASC")
        else:
            cursor.execute(
                "SELECT * FROM divisions WHERE is_active = ? ORDER BY sort_order ASC, id ASC",
                (1 if is_active else 0,)
            )
        return [dict(r) for r in cursor.fetchall()]


def get_division(division_id: int) -> dict | None:
    """Retrieve a single division by ID."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM divisions WHERE id = ?", (division_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_division_by_code(code: str) -> dict | None:
    """Retrieve a single division by unique code."""
    if not code:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM divisions WHERE UPPER(code) = UPPER(?)", (code.strip(),))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_division(division_id: int, **kwargs) -> None:
    """Update division attributes dynamically (name, code, topic_id, group_chat_id, is_active, sort_order)."""
    allowed_keys = {"name", "code", "tournament_id", "topic_id", "group_chat_id", "is_active", "sort_order"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_keys}
    if not updates:
        return
    set_clause = ", ".join(f"{k} = ?" for k in updates.keys())
    values = list(updates.values()) + [division_id]
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(f"UPDATE divisions SET {set_clause} WHERE id = ?", values)


def set_division_group(division_id: int, chat_id: int) -> None:
    """Bind a Telegram group (supergroup chat_id) to a division."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE divisions SET group_chat_id = ? WHERE id = ?",
            (chat_id, division_id)
        )


def get_division_by_group(chat_id: int) -> dict | None:
    """Retrieve division bound to the given group chat_id."""
    if not chat_id:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM divisions WHERE group_chat_id = ? LIMIT 1",
            (chat_id,)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)
        # Fallback: check if division_topics has this group_chat_id
        cursor.execute("""
            SELECT d.* FROM divisions d
            JOIN division_topics dt ON dt.division_id = d.id
            WHERE dt.group_chat_id = ?
            ORDER BY dt.id DESC LIMIT 1
        """, (chat_id,))
        fallback_row = cursor.fetchone()
        return dict(fallback_row) if fallback_row else None


def get_division_group_chat_id(division_id: int | None) -> int | None:
    """
    Group chat a division lives in, or None if it is not bound to one.

    Prefers the explicit divisions.group_chat_id; if the group was never bound
    directly (topics were assigned one by one), falls back to the chat that most
    of the division's topics sit in.
    """
    if not division_id:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT group_chat_id FROM divisions WHERE id = ?", (division_id,))
        row = cursor.fetchone()
        if row and row["group_chat_id"]:
            return int(row["group_chat_id"])

        cursor.execute("""
            SELECT group_chat_id, COUNT(*) AS bindings
            FROM division_topics
            WHERE division_id = ? AND group_chat_id IS NOT NULL
            GROUP BY group_chat_id
            ORDER BY bindings DESC, group_chat_id ASC
            LIMIT 1
        """, (division_id,))
        row = cursor.fetchone()
        return int(row["group_chat_id"]) if row and row["group_chat_id"] else None


def assign_user_division(telegram_id: int, division_id: int | None) -> None:
    """Assign or unassign (if None) a user to a division."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET division_id = ? WHERE telegram_id = ?",
            (division_id, telegram_id)
        )


def get_division_users(division_id: int | None, with_team_only: bool = False) -> list[dict]:
    """
    Retrieve all users belonging to a specific division.
    If division_id is None, returns legacy users without assigned division.
    If with_team_only is True, returns only users who have a team_name assigned.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        if division_id is None:
            if with_team_only:
                cursor.execute("""
                    SELECT telegram_id, username, team_name, league_name, role, division_id, warn_count, squad_photo_id
                    FROM users
                    WHERE division_id IS NULL AND team_name IS NOT NULL AND team_name != ''
                    ORDER BY team_name ASC
                """)
            else:
                cursor.execute("""
                    SELECT telegram_id, username, team_name, league_name, role, division_id, warn_count, squad_photo_id
                    FROM users
                    WHERE division_id IS NULL
                    ORDER BY COALESCE(team_name, username, CAST(telegram_id AS TEXT)) ASC
                """)
        else:
            if with_team_only:
                cursor.execute("""
                    SELECT telegram_id, username, team_name, league_name, role, division_id, warn_count, squad_photo_id
                    FROM users
                    WHERE division_id = ? AND team_name IS NOT NULL AND team_name != ''
                    ORDER BY team_name ASC
                """, (division_id,))
            else:
                cursor.execute("""
                    SELECT telegram_id, username, team_name, league_name, role, division_id, warn_count, squad_photo_id
                    FROM users
                    WHERE division_id = ?
                    ORDER BY CASE WHEN team_name IS NOT NULL AND team_name != '' THEN 0 ELSE 1 END,
                             COALESCE(team_name, username, CAST(telegram_id AS TEXT)) ASC
                """, (division_id,))
        return [dict(r) for r in cursor.fetchall()]


# ─── LOGOVO: Canonical Division Topics & Roles ──────────────────────────────

CANONICAL_TOPIC_TYPES = {
    "draft": "draft",
    "drafts": "draft",
    "черновик": "draft",
    "previews": "previews",
    "preview": "previews",
    "преды": "previews",
    "results": "results",
    "result": "results",
    "результаты": "results",
    "reports": "reports",
    "report": "reports",
    "отчеты": "reports",
    "отчёты": "reports",
    "lineups": "lineups",
    "lineup": "lineups",
    "squads": "lineups",
    "squad": "lineups",
    "составы": "lineups",
    "analytics": "analytics",
    "analytic": "analytics",
    "аналитика": "analytics",
    "tables": "tables",
    "таблицы": "tables",
    "warns": "warns",
    "варны": "warns"
}

TOPIC_DISPLAY_NAMES = {
    "draft": "🏟 ЧЕРНОВИК",
    "previews": "👤 ПРЕДЫ",
    "results": "🎛 РЕЗУЛЬТАТЫ",
    "reports": "📞 ОТЧЁТЫ",
    "lineups": "🗺 СОСТАВЫ",
    "analytics": "📈 АНАЛИТИКА",
    "tables": "📊 ТАБЛИЦЫ",
    "warns": "⚠️ ПРЕДУПРЕЖДЕНИЯ"
}

# The topics that actually exist in a division's forum, in display order.
# "tables" and "warns" are still valid routing keys (read by handlers/base.py
# and handlers/admin.py with a fallback), but no group has such a topic, so the
# admin panel does not list them.
PRIMARY_DIVISION_TOPICS = ["draft", "previews", "results", "reports", "lineups", "analytics"]

def normalize_topic_type(topic_type: str) -> str:
    raw = str(topic_type).strip().lower()
    return CANONICAL_TOPIC_TYPES.get(raw, raw)


def is_division_admin(user_id: int, division_id: int) -> bool:
    """
    Check if user is allowed to manage the given division.
    True if:
    1. User is Global Admin (in ADMIN_IDS or role == 'admin' with division_id IS NULL)
    2. User is explicitly registered in division_admins table
    3. User has role in ('admin', 'division_admin') and users.division_id == division_id
    """
    if not user_id:
        return False
    from config import ADMIN_IDS
    if user_id in ADMIN_IDS:
        return True

    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT role, division_id FROM users WHERE telegram_id = ?", (user_id,))
        u = cursor.fetchone()
        if u:
            if u["role"] == "admin" and (u["division_id"] is None or u["division_id"] == division_id):
                return True
            if u["role"] in ("admin", "division_admin") and u["division_id"] == division_id:
                return True

        cursor.execute("SELECT 1 FROM division_admins WHERE division_id = ? AND user_id = ?", (division_id, user_id))
        if cursor.fetchone():
            return True

    return False


def add_division_admin(division_id: int, user_id: int) -> None:
    """Grant division admin privileges to a user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO division_admins (division_id, user_id, created_at)
            VALUES (?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(division_id, user_id) DO NOTHING
        """, (division_id, user_id))


def remove_division_admin(division_id: int, user_id: int) -> None:
    """Revoke division admin privileges from a user."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM division_admins WHERE division_id = ? AND user_id = ?", (division_id, user_id))


def get_admin_candidate_ids() -> list[int]:
    """Все, кто в базе помечен админом: роль в `users` или строка `division_admins`.

    Кандидаты, а не решение: права проверяет `handlers.base`, а список нужен
    тому, кто должен обойти всех админов (меню команд бота).
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT telegram_id AS user_id FROM users WHERE role IN ('admin', 'division_admin')
            UNION
            SELECT user_id FROM division_admins
        """)
        return [int(r["user_id"]) for r in cursor.fetchall() if r["user_id"]]


def get_division_admins(division_id: int) -> list[int]:
    """List all admin user_ids for a division."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id FROM division_admins WHERE division_id = ?", (division_id,))
        return [r["user_id"] for r in cursor.fetchall()]


def get_division_admins_detailed(division_id: int) -> list[dict]:
    """List division admins together with their profile info (username, team name)."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT da.user_id, u.username, u.team_name
            FROM division_admins da
            LEFT JOIN users u ON u.telegram_id = da.user_id
            WHERE da.division_id = ?
            ORDER BY LOWER(COALESCE(u.username, '')) ASC, da.user_id ASC
        """, (division_id,))
        return [dict(r) for r in cursor.fetchall()]


def get_admin_divisions(user_id: int) -> list[dict]:
    """
    List the divisions a user is bound to in `division_admins`.
    Returns full division rows (empty list for global admins that have no explicit binding).
    """
    if not user_id:
        return []
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT d.*
            FROM division_admins da
            JOIN divisions d ON d.id = da.division_id
            WHERE da.user_id = ?
            ORDER BY d.sort_order ASC, d.id ASC
        """, (user_id,))
        return [dict(r) for r in cursor.fetchall()]


def get_division_rounds(division_id: int) -> list[int]:
    """Get all round numbers that have matches in the given division (active season)."""
    with transaction() as conn:
        cursor = conn.cursor()
        act = get_active_season()
        s_id = act["id"] if act else 1
        cursor.execute("""
            SELECT DISTINCT round_number
            FROM matches
            WHERE round_number > 0
              AND (season_id = ? OR season_id IS NULL)
              AND division_id = ?
            ORDER BY round_number ASC
        """, (s_id, division_id))
        return [row["round_number"] for row in cursor.fetchall()]


def bind_division_topic(
    division_id: int, 
    group_chat_id: int, 
    message_thread_id: int, 
    topic_type: str, 
    force: bool = False
) -> dict:
    """
    Safely bind a Telegram forum topic (group_chat_id, message_thread_id) to a division.
    Enforces isolation, prevents duplicate bindings, and protects against cross-division topic theft.
    """
    norm_type = normalize_topic_type(topic_type)
    valid_types = set(CANONICAL_TOPIC_TYPES.values())
    if norm_type not in valid_types:
        return {"status": "error", "error": f"Invalid topic type: {topic_type}"}

    with transaction() as conn:
        cursor = conn.cursor()
        
        # 1. Validate division exists
        cursor.execute("SELECT id, name FROM divisions WHERE id = ?", (division_id,))
        div_row = cursor.fetchone()
        if not div_row:
            return {"status": "error", "error": f"Division {division_id} not found"}

        # 2. Check current binding of this exact topic (group_chat_id, message_thread_id)
        cursor.execute("""
            SELECT id, division_id, topic_type 
            FROM division_topics 
            WHERE group_chat_id = ? AND message_thread_id = ?
        """, (group_chat_id, message_thread_id))
        existing_topic = cursor.fetchone()

        # Idempotency: same topic already assigned to this exact division and type
        if existing_topic:
            e_div = existing_topic["division_id"]
            e_type = normalize_topic_type(existing_topic["topic_type"])
            if e_div == division_id and e_type == norm_type:
                return {
                    "status": "already_bound",
                    "division_id": division_id,
                    "division_name": div_row["name"],
                    "topic_type": norm_type,
                    "group_chat_id": group_chat_id,
                    "message_thread_id": message_thread_id
                }
            elif not force:
                # Conflict Scenario 1: topic belongs to another division or another type
                cursor.execute("SELECT name FROM divisions WHERE id = ?", (e_div,))
                other_div_row = cursor.fetchone()
                other_div_name = other_div_row["name"] if other_div_row else f"ID {e_div}"
                return {
                    "status": "conflict_topic",
                    "current_division_id": e_div,
                    "current_division_name": other_div_name,
                    "current_topic_type": e_type,
                    "requested_division_id": division_id,
                    "requested_division_name": div_row["name"],
                    "requested_topic_type": norm_type,
                    "group_chat_id": group_chat_id,
                    "message_thread_id": message_thread_id
                }

        # 3. Check if division already has a topic assigned for this topic_type
        cursor.execute("""
            SELECT id, group_chat_id, message_thread_id 
            FROM division_topics 
            WHERE division_id = ? AND (topic_type = ? OR topic_type = ?)
        """, (division_id, norm_type, "drafts" if norm_type == "draft" else norm_type))
        existing_type = cursor.fetchone()

        if existing_type and (existing_type["group_chat_id"] != group_chat_id or existing_type["message_thread_id"] != message_thread_id):
            if not force:
                # Conflict Scenario 2: division already has this topic type bound elsewhere
                return {
                    "status": "conflict_type",
                    "current_chat_id": existing_type["group_chat_id"],
                    "current_thread_id": existing_type["message_thread_id"],
                    "requested_chat_id": group_chat_id,
                    "requested_thread_id": message_thread_id,
                    "division_id": division_id,
                    "division_name": div_row["name"],
                    "topic_type": norm_type
                }

        # 4. If force, clean up old conflicting records
        if force:
            cursor.execute("""
                DELETE FROM division_topics 
                WHERE group_chat_id = ? AND message_thread_id = ?
            """, (group_chat_id, message_thread_id))
            cursor.execute("""
                DELETE FROM division_topics 
                WHERE division_id = ? AND (topic_type = ? OR topic_type = ?)
            """, (division_id, norm_type, "drafts" if norm_type == "draft" else norm_type))

        # 5. Insert or update new binding
        cursor.execute("""
            INSERT INTO division_topics (division_id, topic_type, message_thread_id, group_chat_id, created_at)
            VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(division_id, topic_type) DO UPDATE SET
                message_thread_id = excluded.message_thread_id,
                group_chat_id = excluded.group_chat_id
        """, (division_id, norm_type, message_thread_id, group_chat_id))

        return {
            "status": "ok",
            "division_id": division_id,
            "division_name": div_row["name"],
            "topic_type": norm_type,
            "group_chat_id": group_chat_id,
            "message_thread_id": message_thread_id
        }


def unbind_division_topic(group_chat_id: int, message_thread_id: int) -> dict | None:
    """Unbind a topic by its (group_chat_id, message_thread_id). Returns unbind info or None."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dt.id, dt.division_id, dt.topic_type, dt.group_chat_id, dt.message_thread_id, d.name as division_name
            FROM division_topics dt
            LEFT JOIN divisions d ON dt.division_id = d.id
            WHERE dt.group_chat_id = ? AND dt.message_thread_id = ?
        """, (group_chat_id, message_thread_id))
        row = cursor.fetchone()
        if not row:
            return None
        info = dict(row)
        cursor.execute("DELETE FROM division_topics WHERE id = ?", (row["id"],))
        return info


def get_topic_binding(group_chat_id: int | None, message_thread_id: int) -> dict | None:
    """Retrieve full binding record strictly for a given chat and thread."""
    if group_chat_id is None:
        return None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dt.*, d.name as division_name, d.code as division_code
            FROM division_topics dt
            JOIN divisions d ON dt.division_id = d.id
            WHERE dt.group_chat_id = ? AND dt.message_thread_id = ?
            ORDER BY dt.id DESC LIMIT 1
        """, (group_chat_id, message_thread_id))
        row = cursor.fetchone()
        if row:
            d = dict(row)
            d["topic_type"] = normalize_topic_type(d["topic_type"])
            return d
        return None


def get_division_topics_map(division_id: int) -> dict[str, dict]:
    """Retrieve mapping of topic_type -> {group_chat_id, message_thread_id} for a division."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT topic_type, group_chat_id, message_thread_id
            FROM division_topics
            WHERE division_id = ?
        """, (division_id,))
        res = {}
        for r in cursor.fetchall():
            res[normalize_topic_type(r["topic_type"])] = {
                "group_chat_id": r["group_chat_id"],
                "message_thread_id": r["message_thread_id"]
            }
        return res


def get_all_division_topics() -> list[dict]:
    """Retrieve all division topic bindings across all divisions."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT dt.*, d.name as division_name, d.code as division_code
            FROM division_topics dt
            LEFT JOIN divisions d ON dt.division_id = d.id
            ORDER BY d.sort_order ASC, dt.division_id ASC, dt.topic_type ASC
        """)
        rows = []
        for r in cursor.fetchall():
            item = dict(r)
            item["topic_type"] = normalize_topic_type(item["topic_type"])
            rows.append(item)
        return rows


def set_division_topic(
    division_id: int, 
    topic_type: str, 
    message_thread_id: int | None,
    group_chat_id: int | None = None
) -> None:
    """
    Register or update a Telegram forum topic for a division.
    If message_thread_id is None, removes the topic mapping.
    """
    norm_type = normalize_topic_type(topic_type)
    with transaction() as conn:
        cursor = conn.cursor()
        if message_thread_id is None:
            cursor.execute("""
                DELETE FROM division_topics
                WHERE division_id = ? AND (topic_type = ? OR topic_type = ?)
            """, (division_id, norm_type, topic_type))
        else:
            if group_chat_id is not None:
                cursor.execute("""
                    DELETE FROM division_topics
                    WHERE group_chat_id = ? AND message_thread_id = ?
                """, (group_chat_id, message_thread_id))
                # Тема, бывшая темой кубка, перестаёт ею быть (см. set_cup_topic).
                cursor.execute(
                    "DELETE FROM cup_topics WHERE group_chat_id = ? AND message_thread_id = ?",
                    (group_chat_id, message_thread_id)
                )
            cursor.execute("""
                INSERT INTO division_topics (division_id, topic_type, message_thread_id, group_chat_id, created_at)
                VALUES (?, ?, ?, ?, datetime('now', '+3 hours'))
                ON CONFLICT(division_id, topic_type) DO UPDATE SET 
                    message_thread_id = excluded.message_thread_id,
                    group_chat_id = COALESCE(excluded.group_chat_id, division_topics.group_chat_id)
            """, (division_id, norm_type, message_thread_id, group_chat_id))


def get_division_topic(division_id: int, topic_type: str, group_chat_id: int | None = None) -> int | None:
    """Retrieve message_thread_id for a division's specific topic type."""
    norm_type = normalize_topic_type(topic_type)
    with transaction() as conn:
        cursor = conn.cursor()
        if group_chat_id is not None:
            cursor.execute("""
                SELECT message_thread_id FROM division_topics
                WHERE division_id = ? AND (topic_type = ? OR topic_type = ?) AND (group_chat_id = ? OR group_chat_id IS NULL)
                ORDER BY group_chat_id DESC LIMIT 1
            """, (division_id, norm_type, topic_type, group_chat_id))
        else:
            cursor.execute("""
                SELECT message_thread_id FROM division_topics
                WHERE division_id = ? AND (topic_type = ? OR topic_type = ?)
                LIMIT 1
            """, (division_id, norm_type, topic_type))
        row = cursor.fetchone()
        return row["message_thread_id"] if row else None


def get_division_by_topic(message_thread_id: int, topic_type: str = "drafts", group_chat_id: int | None = None) -> dict | None:
    """
    Find which division owns the given message_thread_id for the given topic_type.
    Returns division dictionary or None.
    """
    norm_type = normalize_topic_type(topic_type)
    with transaction() as conn:
        cursor = conn.cursor()
        if group_chat_id is not None:
            cursor.execute("""
                SELECT d.*
                FROM divisions d
                JOIN division_topics dt ON d.id = dt.division_id
                WHERE dt.message_thread_id = ? 
                  AND (dt.topic_type = ? OR dt.topic_type = ?)
                  AND dt.group_chat_id = ?
                LIMIT 1
            """, (message_thread_id, norm_type, topic_type, group_chat_id))
        else:
            cursor.execute("""
                SELECT d.*
                FROM divisions d
                JOIN division_topics dt ON d.id = dt.division_id
                WHERE dt.message_thread_id = ? 
                  AND (dt.topic_type = ? OR dt.topic_type = ?)
                  AND dt.group_chat_id IS NULL
                LIMIT 1
            """, (message_thread_id, norm_type, topic_type))
        row = cursor.fetchone()
        return dict(row) if row else None


# ─── Phase 7: AI & Sports Intelligence Database Helpers ──────────────────────

def get_team_elo(team_name: str, division_id: int = 1, season_id: int = 1) -> float:
    """Retrieve persisted Elo rating for a team, defaulting to 1500.0 if not tracked."""
    if not team_name:
        return 1500.0
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT elo_rating FROM team_ratings
            WHERE LOWER(team_name) = LOWER(?) AND division_id = ? AND season_id = ?
        """, (team_name, division_id, season_id))
        row = cursor.fetchone()
        return float(row["elo_rating"]) if row else 1500.0


def update_team_elo(
    team_name: str,
    division_id: int,
    season_id: int,
    new_elo: float,
    matches_counted: int | None = None
) -> None:
    """Upsert persisted Elo rating for a team within a division and season."""
    if not team_name:
        return
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, matches_counted FROM team_ratings
            WHERE LOWER(team_name) = LOWER(?) AND division_id = ? AND season_id = ?
        """, (team_name, division_id, season_id))
        existing = cursor.fetchone()

        if existing:
            cnt = matches_counted if matches_counted is not None else (existing["matches_counted"] + 1)
            cursor.execute("""
                UPDATE team_ratings
                SET elo_rating = ?, matches_counted = ?, last_updated_at = datetime('now', '+3 hours')
                WHERE id = ?
            """, (round(float(new_elo), 2), cnt, existing["id"]))
        else:
            cnt = matches_counted if matches_counted is not None else 1
            cursor.execute("""
                INSERT INTO team_ratings (team_name, division_id, season_id, elo_rating, matches_counted, last_updated_at)
                VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            """, (team_name, division_id, season_id, round(float(new_elo), 2), cnt))


def _elo_team_names(cursor: sqlite3.Cursor, match_id: int) -> dict | None:
    """Read the columns _apply_elo_after_match needs, or None when the match is unusable."""
    cursor.execute(
        "SELECT player1_team, player2_team, division_id, season_id, is_technical "
        "FROM matches WHERE id = ?",
        (match_id,)
    )
    row = cursor.fetchone()
    if not row:
        return None
    t1 = (row["player1_team"] or "").strip()
    t2 = (row["player2_team"] or "").strip()
    if not t1 or not t2:
        return None
    return {
        "team1": t1,
        "team2": t2,
        "division_id": row["division_id"] or 1,
        "season_id": row["season_id"] or 1,
        "is_technical": bool(row["is_technical"]),
    }


def _elo_set_rating(cursor: sqlite3.Cursor, team_name: str, division_id: int,
                    season_id: int, new_elo: float, bump_counter: bool) -> None:
    """Write an Elo rating, controlling whether matches_counted advances.

    A correction rewrites the same match, so the counter must stay put — otherwise
    one played match would be counted twice.
    """
    cursor.execute(
        "SELECT id, matches_counted FROM team_ratings "
        "WHERE LOWER(team_name) = LOWER(?) AND division_id = ? AND season_id = ?",
        (team_name, division_id, season_id)
    )
    existing = cursor.fetchone()
    value = round(float(new_elo), 2)
    if existing:
        cnt = existing["matches_counted"] + (1 if bump_counter else 0)
        cursor.execute(
            "UPDATE team_ratings SET elo_rating = ?, matches_counted = ?, "
            "last_updated_at = datetime('now', '+3 hours') WHERE id = ?",
            (value, cnt, existing["id"])
        )
    else:
        cursor.execute(
            "INSERT INTO team_ratings (team_name, division_id, season_id, elo_rating, matches_counted, last_updated_at) "
            "VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))",
            (team_name, division_id, season_id, value, 1 if bump_counter else 0)
        )


def _apply_elo_after_match(match_id: int, p1_score: int, p2_score: int) -> bool:
    """Move both clubs' Elo ratings after a confirmed result.

    Idempotent by design: `elo_applied_matches` stores the deltas this match
    produced, so a later admin score correction first reverses them and then
    applies the new ones instead of stacking a second update on top.

    Technical results (ТП/ТН) are skipped — they are an administrative verdict,
    not a played match, and must not move sporting ratings.
    """
    from services.elo_engine import EloEngine

    with transaction() as conn:
        cursor = conn.cursor()
        info = _elo_team_names(cursor, match_id)
        if info is None:
            return False
        if info["is_technical"]:
            return False

        team1, team2 = info["team1"], info["team2"]
        division_id, season_id = info["division_id"], info["season_id"]

        cursor.execute(
            "SELECT delta1, delta2 FROM elo_applied_matches WHERE match_id = ?",
            (match_id,)
        )
        prior = cursor.fetchone()

        r1 = get_team_elo(team1, division_id, season_id)
        r2 = get_team_elo(team2, division_id, season_id)

        if prior:
            # Rewind this match's own contribution before recomputing it.
            r1 -= float(prior["delta1"])
            r2 -= float(prior["delta2"])

        new_r1, new_r2 = EloEngine.calculate_new_ratings(r1, r2, p1_score, p2_score)
        delta1 = round(new_r1 - r1, 2)
        delta2 = round(new_r2 - r2, 2)

        bump = prior is None
        _elo_set_rating(cursor, team1, division_id, season_id, new_r1, bump)
        _elo_set_rating(cursor, team2, division_id, season_id, new_r2, bump)

        cursor.execute("""
            INSERT INTO elo_applied_matches (match_id, team1, team2, delta1, delta2, applied_at)
            VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(match_id) DO UPDATE SET
                team1 = excluded.team1,
                team2 = excluded.team2,
                delta1 = excluded.delta1,
                delta2 = excluded.delta2,
                applied_at = datetime('now', '+3 hours')
        """, (match_id, team1, team2, delta1, delta2))

    logger.info(
        "Elo applied for match #%s: %s %+.2f, %s %+.2f",
        match_id, team1, delta1, team2, delta2
    )
    return True


def save_ai_prediction(
    match_id: int,
    division_id: int,
    season_id: int,
    model_version: str,
    feature_version: str,
    home_prob: float,
    draw_prob: float,
    away_prob: float,
    confidence: float,
    over_1_5: float | None = None,
    over_2_5: float | None = None,
    over_3_5: float | None = None,
    btts_yes: float | None = None,
    btts_no: float | None = None,
    key_factors: list[str] | None = None
) -> int:
    """Persist an AI model prediction record.

    (match_id, model_version) — одна запись: повторный вызов возвращает id уже
    сохранённого прогноза, новую строку не создаёт и исторические поля не трогает.
    """
    import json as _json
    factors_json = _json.dumps(key_factors or [], ensure_ascii=False)
    values = (
        match_id, division_id, season_id, model_version, feature_version,
        round(home_prob, 4), round(draw_prob, 4), round(away_prob, 4),
        round(over_1_5, 4) if over_1_5 is not None else None,
        round(over_2_5, 4) if over_2_5 is not None else None,
        round(over_3_5, 4) if over_3_5 is not None else None,
        round(btts_yes, 4) if btts_yes is not None else None,
        round(btts_no, 4) if btts_no is not None else None,
        round(confidence, 4), factors_json
    )
    with transaction() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT INTO predictions (
                    match_id, division_id, season_id, model_version, feature_version,
                    home_probability, draw_probability, away_probability,
                    over_1_5_probability, over_2_5_probability, over_3_5_probability,
                    btts_yes_probability, btts_no_probability,
                    confidence, key_factors, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
                ON CONFLICT(match_id, model_version) DO NOTHING
            """, values)
        except sqlite3.OperationalError as exc:
            # Пока уникального индекса нет (миграция 019 на проде не прошла — ей
            # мешают исторические дубли), ON CONFLICT(...) не компилируется уже на
            # подготовке. Фолбэк даёт ту же атомарность одним оператором:
            # SELECT-then-INSERT оставил бы окно между запросами.
            if "ON CONFLICT clause does not match" not in str(exc):
                raise
            cursor.execute("""
                INSERT INTO predictions (
                    match_id, division_id, season_id, model_version, feature_version,
                    home_probability, draw_probability, away_probability,
                    over_1_5_probability, over_2_5_probability, over_3_5_probability,
                    btts_yes_probability, btts_no_probability,
                    confidence, key_factors, created_at
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours')
                WHERE NOT EXISTS (
                    SELECT 1 FROM predictions WHERE match_id = ? AND model_version = ?
                )
            """, values + (match_id, model_version))
        if cursor.rowcount:
            return int(cursor.lastrowid)
        # Вставка заблокирована существующей строкой: отдаём её id — тот же, что
        # вернёт get_ai_prediction (он тоже берёт последний по id).
        cursor.execute("""
            SELECT id FROM predictions
            WHERE match_id = ? AND model_version = ?
            ORDER BY id DESC LIMIT 1
        """, (match_id, model_version))
        row = cursor.fetchone()
        if row is None:
            raise RuntimeError(
                f"Prediction for match #{match_id} ({model_version}) vanished inside its own transaction"
            )
        return int(row["id"])


def get_ai_prediction(match_id: int, model_version: str = "ensemble_v1") -> dict | None:
    """Retrieve latest stored AI prediction for a match."""
    import json as _json
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM predictions
            WHERE match_id = ? AND model_version = ?
            ORDER BY id DESC LIMIT 1
        """, (match_id, model_version))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("key_factors"):
            try:
                res["key_factors"] = _json.loads(res["key_factors"])
            except Exception:
                res["key_factors"] = []
        return res


def save_prediction_snapshot(
    match_id: int,
    stage: str,
    minute: int | None,
    home_score: int,
    away_score: int,
    home_prob: float,
    draw_prob: float,
    away_prob: float,
    confidence: float
) -> int:
    """Save temporal snapshot of prediction at a given game state."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO prediction_snapshots (
                match_id, stage, minute, home_score, away_score,
                home_prob, draw_prob, away_prob, confidence, snapshot_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (
            match_id, stage, minute, home_score, away_score,
            round(home_prob, 4), round(draw_prob, 4), round(away_prob, 4),
            round(confidence, 4)
        ))
        return cursor.lastrowid


def get_prediction_snapshots(match_id: int, limit: int = 20) -> list[dict]:
    """Retrieve chronological snapshots for a match."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM prediction_snapshots
            WHERE match_id = ?
            ORDER BY id ASC LIMIT ?
        """, (match_id, limit))
        return [dict(r) for r in cursor.fetchall()]


def resolve_ai_predictions(match_id: int, home_score: int, away_score: int) -> int:
    """
    Resolve pending predictions after confirmed match result.
    Calculates Brier Score: (p_home - y_home)^2 + (p_draw - y_draw)^2 + (p_away - y_away)^2.
    """
    if home_score is None or away_score is None:
        return 0

    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except (ValueError, TypeError):
        return 0

    if home_score > away_score:
        actual_result = "home"
        y = (1.0, 0.0, 0.0)
    elif home_score == away_score:
        actual_result = "draw"
        y = (0.0, 1.0, 0.0)
    else:
        actual_result = "away"
        y = (0.0, 0.0, 1.0)

    resolved_count = 0
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, home_probability, draw_probability, away_probability
            FROM predictions
            WHERE match_id = ? AND resolved_at IS NULL
        """, (match_id,))
        preds = cursor.fetchall()

        for p in preds:
            p_h = float(p["home_probability"])
            p_d = float(p["draw_probability"])
            p_a = float(p["away_probability"])

            # Multiclass Brier score
            brier = round(((p_h - y[0]) ** 2 + (p_d - y[1]) ** 2 + (p_a - y[2]) ** 2) / 2.0, 4)

            # Determine is_correct
            highest_prob = max(p_h, p_d, p_a)
            predicted_outcome = "home" if highest_prob == p_h else ("draw" if highest_prob == p_d else "away")
            is_correct = (predicted_outcome == actual_result)

            cursor.execute("""
                UPDATE predictions
                SET resolved_at = datetime('now', '+3 hours'), actual_result = ?, is_correct = ?, brier_score = ?
                WHERE id = ?
            """, (actual_result, 1 if is_correct else 0, brier, p["id"]))
            resolved_count += 1

    return resolved_count


def correct_ai_predictions(match_id: int, new_home_score: int, new_away_score: int) -> int:
    """
    Recalculate and correct AI prediction accuracy when a confirmed match result
    undergoes administrative or official score correction.
    Strict Invariant: Re-evaluates AI analytical records without triggering bet settlement or wallet adjustments.
    """
    if new_home_score is None or new_away_score is None:
        return 0

    try:
        new_home_score = int(new_home_score)
        new_away_score = int(new_away_score)
    except (ValueError, TypeError):
        return 0

    if new_home_score > new_away_score:
        actual_result = "home"
        y = (1.0, 0.0, 0.0)
    elif new_home_score == new_away_score:
        actual_result = "draw"
        y = (0.0, 1.0, 0.0)
    else:
        actual_result = "away"
        y = (0.0, 0.0, 1.0)

    updated_count = 0
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, home_probability, draw_probability, away_probability
            FROM predictions
            WHERE match_id = ?
        """, (match_id,))
        preds = cursor.fetchall()

        for p in preds:
            p_h = float(p["home_probability"])
            p_d = float(p["draw_probability"])
            p_a = float(p["away_probability"])

            brier = round(((p_h - y[0]) ** 2 + (p_d - y[1]) ** 2 + (p_a - y[2]) ** 2) / 2.0, 4)
            highest_prob = max(p_h, p_d, p_a)
            predicted_outcome = "home" if highest_prob == p_h else ("draw" if highest_prob == p_d else "away")
            is_correct = (predicted_outcome == actual_result)

            cursor.execute("""
                UPDATE predictions
                SET resolved_at = datetime('now', '+3 hours'), actual_result = ?, is_correct = ?, brier_score = ?
                WHERE id = ?
            """, (actual_result, 1 if is_correct else 0, brier, p["id"]))
            updated_count += 1

    return updated_count


# ─── Integrity Engine: репозиторий дел о договорных матчах ────────────────────

# Общая выборка ноги ставки со всем контекстом, который нужен движку оценки.
# Это константа модуля, а не подстановка данных: значения всегда идут
# параметрами, склейка тут — только с литеральным WHERE ниже.
_INTEGRITY_ITEM_SELECT = """
    SELECT
        bi.id                AS bet_item_id,
        bi.bet_id            AS bet_id,
        bi.match_id          AS match_id,
        bi.outcome_type      AS outcome_type,
        bi.odd               AS odd,
        bi.odds_at_placement AS odds_at_placement,
        bi.market_id         AS market_id,
        bi.selection_id      AS selection_id,
        bi.status            AS item_status,
        ub.user_id           AS user_id,
        ub.amount            AS amount,
        ub.bet_type          AS bet_type,
        ub.status            AS bet_status,
        ub.actual_payout     AS actual_payout,
        ub.total_odd         AS total_odd,
        ub.created_at        AS placed_at,
        u.username           AS username,
        u.team_name          AS user_team,
        m.division_id        AS division_id,
        m.season_id          AS season_id,
        m.round_number       AS round_number,
        m.status             AS match_status,
        m.is_technical       AS is_technical,
        m.player1_team       AS player1_team,
        m.player2_team       AS player2_team,
        m.player1_id         AS player1_id,
        m.player2_id         AS player2_id,
        m.player1_score      AS player1_score,
        m.player2_score      AS player2_score,
        mk.market_key        AS market_key,
        mk.market_name       AS market_name,
        ms.selection_key     AS selection_key,
        ms.selection_name    AS selection_name,
        ms.odds_value        AS current_odds,
        ms.model_odds        AS model_odds,
        (
            SELECT r.bets_opened_at FROM rounds r
            WHERE r.division_id = m.division_id
              AND r.round_number = m.round_number
              AND (m.season_id IS NULL OR r.season_id = m.season_id)
            ORDER BY r.id LIMIT 1
        ) AS bets_opened_at,
        (
            SELECT ct.balance_after - ct.amount FROM coin_transactions ct
            WHERE ct.reference_type = 'bet'
              AND ct.reference_id = ub.id
              AND ct.transaction_type = 'bet_placed'
            ORDER BY ct.id LIMIT 1
        ) AS balance_before
    FROM bet_items bi
    JOIN user_bets ub ON ub.id = bi.bet_id
    JOIN matches m ON m.id = bi.match_id
    LEFT JOIN users u ON u.telegram_id = ub.user_id
    LEFT JOIN markets mk ON mk.id = bi.market_id
    LEFT JOIN market_selections ms ON ms.id = bi.selection_id
"""


def get_unscored_bet_items(limit: int = 50, min_stake: int | None = None) -> list[dict]:
    """
    Ноги ставок, для которых дела ещё нет: вход онлайн-прохода детектора.

    Фильтр входа — главная защита от ложных срабатываний: внешние фикстуры
    (без division_id), мелкие ставки и уже отменённые ставки не оцениваются.
    """
    if min_stake is None:
        # Локальный импорт, чтобы порог читался на каждом проходе джобы,
        # а не замораживался на момент импорта database.
        import config as _config
        min_stake = getattr(_config, "INTEGRITY_MIN_STAKE", 500)

    sql = _INTEGRITY_ITEM_SELECT + """
    WHERE m.division_id IS NOT NULL
      AND ub.amount >= ?
      AND ub.status NOT IN ('cancelled', 'refunded')
      AND NOT EXISTS (
          SELECT 1 FROM integrity_cases ic
          WHERE ic.bet_id = bi.bet_id AND ic.bet_item_id = bi.id
      )
    ORDER BY bi.id DESC
    LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.cursor().execute(sql, (int(min_stake), int(limit))).fetchall()
    return [dict(r) for r in rows]


def get_resolvable_integrity_cases(limit: int = 50) -> list[dict]:
    """
    Дела на стадии 'online', чей матч уже подтверждён, а нога рассчитана:
    вход постматчевого прохода.
    """
    sql = _INTEGRITY_ITEM_SELECT + """
    JOIN integrity_cases ic ON ic.bet_id = bi.bet_id AND ic.bet_item_id = bi.id
    WHERE ic.stage = 'online'
      AND bi.status IN ('won', 'lost', 'refunded')
      AND m.status IN ('confirmed', 'finished')
      AND m.player1_score IS NOT NULL
      AND m.player2_score IS NOT NULL
    ORDER BY ic.id ASC
    LIMIT ?
    """
    with get_connection() as conn:
        rows = conn.cursor().execute(sql, (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def get_market_odds_snapshot(market_id: int) -> list[float]:
    """Текущие коэффициенты всех живых исходов рынка — для расчёта маржи."""
    if not market_id:
        return []
    with get_connection() as conn:
        rows = conn.cursor().execute(
            "SELECT odds_value FROM market_selections WHERE market_id = ? AND status != 'voided'",
            (int(market_id),)
        ).fetchall()
    return [float(r["odds_value"]) for r in rows if r["odds_value"]]


def get_user_bet_profile(user_id: int, before: str | None = None, limit: int = 30) -> dict:
    """
    Поведенческий профиль игрока на момент ставки: суммы предыдущих ставок,
    какие рынки он уже брал и когда ставил в прошлый раз.

    `before` — created_at оцениваемой ставки: профиль всегда строится по
    прошлому, иначе ставка оценивала бы саму себя.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        if before:
            rows = cursor.execute("""
                SELECT amount, created_at FROM user_bets
                WHERE user_id = ? AND created_at < ?
                ORDER BY created_at DESC LIMIT ?
            """, (user_id, before, int(limit))).fetchall()
            market_rows = cursor.execute("""
                SELECT mk.market_key AS market_key, COUNT(*) AS n
                FROM bet_items bi
                JOIN user_bets ub ON ub.id = bi.bet_id
                JOIN markets mk ON mk.id = bi.market_id
                WHERE ub.user_id = ? AND ub.created_at < ?
                GROUP BY mk.market_key
            """, (user_id, before)).fetchall()
            last_row = cursor.execute("""
                SELECT MAX(created_at) AS last_at FROM user_bets
                WHERE user_id = ? AND created_at < ?
            """, (user_id, before)).fetchone()
        else:
            rows = cursor.execute("""
                SELECT amount, created_at FROM user_bets
                WHERE user_id = ? ORDER BY created_at DESC LIMIT ?
            """, (user_id, int(limit))).fetchall()
            market_rows = cursor.execute("""
                SELECT mk.market_key AS market_key, COUNT(*) AS n
                FROM bet_items bi
                JOIN user_bets ub ON ub.id = bi.bet_id
                JOIN markets mk ON mk.id = bi.market_id
                WHERE ub.user_id = ?
                GROUP BY mk.market_key
            """, (user_id,)).fetchall()
            last_row = cursor.execute(
                "SELECT MAX(created_at) AS last_at FROM user_bets WHERE user_id = ?",
                (user_id,)
            ).fetchone()

    return {
        "amounts": [int(r["amount"]) for r in rows if r["amount"] is not None],
        "bets_count": len(rows),
        "market_counts": {r["market_key"]: int(r["n"]) for r in market_rows if r["market_key"]},
        "last_bet_at": last_row["last_at"] if last_row else None,
    }


def get_selection_volume(match_id: int, selection_id: int | None, exclude_user_id: int | None = None) -> dict:
    """
    Оборот по матчу и по конкретному исходу: сколько ещё игроков зашли туда же.
    Свою ставку исключаем — интересует поведение остальных.
    """
    result = {"selection_users": 0, "selection_amount": 0, "match_users": 0, "match_amount": 0}
    with get_connection() as conn:
        cursor = conn.cursor()
        row = cursor.execute("""
            SELECT COUNT(DISTINCT ub.user_id) AS users, COALESCE(SUM(ub.amount), 0) AS total
            FROM bet_items bi
            JOIN user_bets ub ON ub.id = bi.bet_id
            WHERE bi.match_id = ? AND ub.status NOT IN ('cancelled', 'refunded')
              AND (? IS NULL OR ub.user_id != ?)
        """, (match_id, exclude_user_id, exclude_user_id)).fetchone()
        if row:
            result["match_users"] = int(row["users"] or 0)
            result["match_amount"] = int(row["total"] or 0)

        if selection_id:
            row = cursor.execute("""
                SELECT COUNT(DISTINCT ub.user_id) AS users, COALESCE(SUM(ub.amount), 0) AS total
                FROM bet_items bi
                JOIN user_bets ub ON ub.id = bi.bet_id
                WHERE bi.match_id = ? AND bi.selection_id = ?
                  AND ub.status NOT IN ('cancelled', 'refunded')
                  AND (? IS NULL OR ub.user_id != ?)
            """, (match_id, selection_id, exclude_user_id, exclude_user_id)).fetchone()
            if row:
                result["selection_users"] = int(row["users"] or 0)
                result["selection_amount"] = int(row["total"] or 0)

    return result


def upsert_integrity_case(
    bet_id: int,
    bet_item_id: int,
    user_id: int,
    match_id: int | None,
    division_id: int | None,
    season_id: int | None,
    online_score: float,
    post_score: float,
    total_score: float,
    severity: str,
    stage: str,
    low_confidence: bool,
    features: str | None
) -> int:
    """
    Создать или обновить дело. Вердикт супер-админа (status / reviewed_by /
    note) переоценка не трогает: разобранное дело не должно всплывать заново.
    """
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO integrity_cases (
                bet_id, bet_item_id, user_id, match_id, division_id, season_id,
                online_score, post_score, total_score, severity, stage,
                low_confidence, features, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                datetime('now', '+3 hours'), datetime('now', '+3 hours')
            )
            ON CONFLICT(bet_id, bet_item_id) DO UPDATE SET
                online_score = excluded.online_score,
                post_score = excluded.post_score,
                total_score = excluded.total_score,
                severity = excluded.severity,
                stage = excluded.stage,
                low_confidence = excluded.low_confidence,
                features = excluded.features,
                updated_at = datetime('now', '+3 hours')
        """, (
            bet_id, bet_item_id, user_id, match_id, division_id, season_id,
            float(online_score), float(post_score), float(total_score),
            severity, stage, 1 if low_confidence else 0, features
        ))
        row = cursor.execute(
            "SELECT id FROM integrity_cases WHERE bet_id = ? AND bet_item_id = ?",
            (bet_id, bet_item_id)
        ).fetchone()
        return int(row["id"]) if row else 0


def get_integrity_cases(
    status: str | None = None,
    severity: str | None = None,
    min_score: float | None = None,
    limit: int = 5,
    offset: int = 0
) -> list[dict]:
    """Список дел для экрана «Подозрения», по убыванию балла."""
    with get_connection() as conn:
        rows = conn.cursor().execute("""
            SELECT
                ic.*,
                u.username   AS username,
                u.team_name  AS user_team,
                m.player1_team AS player1_team,
                m.player2_team AS player2_team,
                m.player1_score AS player1_score,
                m.player2_score AS player2_score,
                ub.amount    AS amount,
                bi.status    AS item_status,
                bi.odds_at_placement AS odds_at_placement,
                bi.outcome_type AS outcome_type,
                ms.selection_name AS selection_name,
                mk.market_name AS market_name
            FROM integrity_cases ic
            LEFT JOIN users u ON u.telegram_id = ic.user_id
            LEFT JOIN matches m ON m.id = ic.match_id
            LEFT JOIN user_bets ub ON ub.id = ic.bet_id
            LEFT JOIN bet_items bi ON bi.id = ic.bet_item_id
            LEFT JOIN market_selections ms ON ms.id = bi.selection_id
            LEFT JOIN markets mk ON mk.id = bi.market_id
            WHERE (? IS NULL OR ic.status = ?)
              AND (? IS NULL OR ic.severity = ?)
              AND (? IS NULL OR ic.total_score >= ?)
            ORDER BY ic.total_score DESC, ic.id DESC
            LIMIT ? OFFSET ?
        """, (
            status, status, severity, severity, min_score, min_score,
            int(limit), int(offset)
        )).fetchall()
    return [dict(r) for r in rows]


def count_integrity_cases(
    status: str | None = None,
    severity: str | None = None,
    min_score: float | None = None
) -> int:
    """Счётчик дел под теми же фильтрами, что и get_integrity_cases."""
    with get_connection() as conn:
        row = conn.cursor().execute("""
            SELECT COUNT(*) AS n FROM integrity_cases
            WHERE (? IS NULL OR status = ?)
              AND (? IS NULL OR severity = ?)
              AND (? IS NULL OR total_score >= ?)
        """, (status, status, severity, severity, min_score, min_score)).fetchone()
    return int(row["n"]) if row else 0


def get_integrity_case(case_id: int) -> dict | None:
    """Одно дело со всем контекстом — для карточки."""
    with get_connection() as conn:
        row = conn.cursor().execute("""
            SELECT
                ic.*,
                u.username   AS username,
                u.team_name  AS user_team,
                m.player1_team AS player1_team,
                m.player2_team AS player2_team,
                m.player1_score AS player1_score,
                m.player2_score AS player2_score,
                m.status     AS match_status,
                ub.amount    AS amount,
                ub.bet_type  AS bet_type,
                ub.status    AS bet_status,
                ub.actual_payout AS actual_payout,
                ub.created_at AS placed_at,
                bi.status    AS item_status,
                bi.odd       AS odd,
                bi.odds_at_placement AS odds_at_placement,
                bi.outcome_type AS outcome_type,
                ms.selection_name AS selection_name,
                mk.market_name AS market_name,
                mk.market_key AS market_key
            FROM integrity_cases ic
            LEFT JOIN users u ON u.telegram_id = ic.user_id
            LEFT JOIN matches m ON m.id = ic.match_id
            LEFT JOIN user_bets ub ON ub.id = ic.bet_id
            LEFT JOIN bet_items bi ON bi.id = ic.bet_item_id
            LEFT JOIN market_selections ms ON ms.id = bi.selection_id
            LEFT JOIN markets mk ON mk.id = bi.market_id
            WHERE ic.id = ?
        """, (int(case_id),)).fetchone()
    return dict(row) if row else None


def set_integrity_case_status(
    case_id: int,
    status: str,
    admin_id: int | None = None,
    note: str | None = None
) -> bool:
    """Вердикт супер-админа по делу."""
    if status not in ("open", "acknowledged", "dismissed", "confirmed"):
        return False
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            UPDATE integrity_cases
            SET status = ?,
                reviewed_by = ?,
                reviewed_at = datetime('now', '+3 hours'),
                note = COALESCE(?, note),
                updated_at = datetime('now', '+3 hours')
            WHERE id = ?
        """, (status, admin_id, note, int(case_id)))
        return cursor.rowcount > 0


# ─── Phase 8: Real Sports Provider Repository Helpers ─────────────────────────

def record_provider_sync_log(
    provider: str,
    endpoint: str,
    status_code: int,
    records_count: int = 0,
    latency_ms: float = 0.0,
    error_message: str | None = None
) -> int:
    """Record an audit entry in provider_sync_log."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO provider_sync_log (
                provider, endpoint, status_code, records_count, latency_ms, error_message, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
        """, (provider, endpoint, status_code, records_count, round(latency_ms, 2), error_message))
        return cursor.lastrowid


def link_provider_match(
    provider: str,
    provider_match_id: str,
    match_id: int,
    division_id: int = 1,
    season_id: int = 1,
    status: str = "SCHEDULED",
    home_score: int = 0,
    away_score: int = 0,
    minute: int | None = None,
    payload: dict | None = None
) -> int:
    """Link an external provider match identifier to an internal match entity."""
    import json as _json
    payload_str = _json.dumps(payload, ensure_ascii=False) if payload else None
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO provider_matches (
                provider, provider_match_id, match_id, division_id, season_id,
                status, home_score, away_score, minute, payload, last_update_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            ON CONFLICT(provider, provider_match_id) DO UPDATE SET
                status = excluded.status,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                minute = excluded.minute,
                payload = excluded.payload,
                last_update_at = datetime('now', '+3 hours')
        """, (provider, str(provider_match_id), match_id, division_id, season_id, status, home_score, away_score, minute, payload_str))
        return cursor.lastrowid


def get_provider_match(provider: str, provider_match_id: str) -> dict | None:
    """Retrieve internal match link for given provider fixture."""
    import json as _json
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM provider_matches
            WHERE provider = ? AND provider_match_id = ?
            LIMIT 1
        """, (provider, str(provider_match_id)))
        row = cursor.fetchone()
        if not row:
            return None
        res = dict(row)
        if res.get("payload"):
            try:
                res["payload"] = _json.loads(res["payload"])
            except Exception:
                pass
        return res


def get_provider_match_by_internal_id(match_id: int) -> dict | None:
    """Retrieve external provider details for given internal match_id."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM provider_matches
            WHERE match_id = ?
            ORDER BY id DESC LIMIT 1
        """, (match_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def update_provider_health_state(
    provider_name: str,
    status: str,
    consecutive_failures: int = 0,
    last_error: str | None = None
) -> None:
    """Update circuit breaker and connectivity status for a sports provider."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sports_providers (
                provider_name, display_name, circuit_breaker_status, consecutive_failures,
                last_sync_at, last_error, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?,
                datetime('now', '+3 hours'), ?,
                datetime('now', '+3 hours'), datetime('now', '+3 hours')
            )
            ON CONFLICT(provider_name) DO UPDATE SET
                circuit_breaker_status = excluded.circuit_breaker_status,
                consecutive_failures = excluded.consecutive_failures,
                last_sync_at = datetime('now', '+3 hours'),
                last_error = excluded.last_error,
                updated_at = datetime('now', '+3 hours')
        """, (provider_name, provider_name.upper(), status, consecutive_failures, last_error))


def get_provider_health_state(provider_name: str) -> dict | None:
    """Fetch stored health state of a sports provider."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT * FROM sports_providers
            WHERE provider_name = ?
            LIMIT 1
        """, (provider_name,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_stale_provider_matches_count(stale_threshold_seconds: int = 120) -> int:
    """Count number of currently open/live matches that haven't received updates within threshold."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT COUNT(*) as cnt
            FROM live_match_states
            WHERE status IN ('LIVE', 'HALFTIME')
              AND (julianday('now', '+3 hours') - julianday(last_updated_at)) * 86400 > ?
        """, (stale_threshold_seconds,))
        row = cursor.fetchone()
        return row["cnt"] if row else 0


def reconcile_all_divisions_data(season_id: int | None = None) -> dict:
    """
    Perform a complete audit and reconciliation of divisions data:
    - Standings mathematical integrity (played = W+D+L, points = 3W+D, sum(W) = sum(L), sum(GF) = sum(GA)).
    - Match scores vs player match_events (goals scored by team vs individual author events).
    - Checks for negative scores, missing events, or discrepancies in active divisions.
    Returns structured audit result with status, metrics, and any discrepancies found.
    """
    with transaction() as conn:
        cursor = conn.cursor()

        target_season_id = season_id
        if target_season_id is None:
            act = get_active_season()
            target_season_id = act["id"] if act else 1

        active_divisions = get_active_divisions()

        total_matches_checked = 0
        total_events_checked = 0
        total_goals_in_matches = 0
        total_goals_in_events = 0
        discrepancies: list[str] = []
        division_summaries: list[dict] = []

        for d in active_divisions:
            div_id = d["id"]
            div_name = d["name"]

            # 1. Standings mathematical consistency
            standings = get_standings(division_id=div_id, season_id=target_season_id)
            sum_w = sum(t["wins"] for t in standings)
            sum_l = sum(t["losses"] for t in standings)
            sum_d = sum(t["draws"] for t in standings)
            sum_gf = sum(t["goals_scored"] for t in standings)
            sum_ga = sum(t["goals_conceded"] for t in standings)

            if sum_w != sum_l:
                discrepancies.append(
                    f"[{div_name}] Баланс побед и поражений: побед={sum_w}, поражений={sum_l}"
                )
            if sum_gf != sum_ga:
                discrepancies.append(
                    f"[{div_name}] Баланс голов: забито={sum_gf}, пропущено={sum_ga}"
                )
            if sum_d % 2 != 0:
                discrepancies.append(
                    f"[{div_name}] Нечётное число ничьих в турнирной таблице: {sum_d}"
                )

            for t in standings:
                t_name = t["team_name"]
                if t["played"] != t["wins"] + t["draws"] + t["losses"]:
                    discrepancies.append(
                        f"[{div_name} • {t_name}] Игры не сходятся: сыграно {t['played']} != {t['wins'] + t['draws'] + t['losses']}"
                    )
                if t["points"] != t["wins"] * 3 + t["draws"]:
                    discrepancies.append(
                        f"[{div_name} • {t_name}] Очки не сходятся: {t['points']} != {t['wins'] * 3 + t['draws']}"
                    )

            # 2. Confirmed matches in this division
            cursor.execute("""
                SELECT 
                    m.id, 
                    m.round_number, 
                    COALESCE(m.player1_team, u1.team_name) AS player1_team, 
                    COALESCE(m.player2_team, u2.team_name) AS player2_team, 
                    m.player1_score, 
                    m.player2_score 
                FROM matches m
                LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
                LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
                WHERE m.status = 'confirmed'
                  AND (m.tournament_type IS NULL OR m.tournament_type = 'league')
                  AND m.division_id = ?
                  AND (m.season_id = ? OR m.season_id IS NULL)
                ORDER BY m.id ASC
            """, (div_id, target_season_id))
            div_matches = cursor.fetchall()

            div_match_goals = 0
            for m in div_matches:
                m_id = m["id"]
                p1_team = m["player1_team"] or "Команда 1"
                p2_team = m["player2_team"] or "Команда 2"
                p1_sc = m["player1_score"] if m["player1_score"] is not None else 0
                p2_sc = m["player2_score"] if m["player2_score"] is not None else 0
                div_match_goals += (p1_sc + p2_sc)
                total_matches_checked += 1

                # Events for this match
                cursor.execute("""
                    SELECT team_name, player_name, event_type, count
                    FROM match_events
                    WHERE match_id = ?
                """, (m_id,))
                events = cursor.fetchall()
                total_events_checked += len(events)

                goal_events = [e for e in events if e["event_type"] == "goal"]
                assist_events = [e for e in events if e["event_type"] == "assist"]

                if goal_events:
                    e_p1 = sum(e["count"] for e in goal_events if teams_match(e["team_name"], p1_team))
                    e_p2 = sum(e["count"] for e in goal_events if teams_match(e["team_name"], p2_team))
                    total_goals_in_events += (e_p1 + e_p2)

                    if e_p1 != p1_sc or e_p2 != p2_sc:
                        discrepancies.append(
                            f"[{div_name}] Матч #{m_id} ({p1_team} {p1_sc}:{p2_sc} {p2_team}): "
                            f"в событиях авторов {e_p1}:{e_p2} голов"
                        )

                if assist_events:
                    a_p1 = sum(e["count"] for e in assist_events if teams_match(e["team_name"], p1_team))
                    a_p2 = sum(e["count"] for e in assist_events if teams_match(e["team_name"], p2_team))
                    if a_p1 > p1_sc:
                        discrepancies.append(
                            f"[{div_name}] Матч #{m_id} ({p1_team}): ассистов ({a_p1}) больше чем голов ({p1_sc})"
                        )
                    if a_p2 > p2_sc:
                        discrepancies.append(
                            f"[{div_name}] Матч #{m_id} ({p2_team}): ассистов ({a_p2}) больше чем голов ({p2_sc})"
                        )

            total_goals_in_matches += div_match_goals

            # Top scorer for division summary
            top_sc = get_top_scorers(limit=1, division_id=div_id, season_id=target_season_id)
            top_scorer_str = f"{top_sc[0]['player_name']} ({top_sc[0]['total_goals']})" if top_sc else "—"

            division_summaries.append({
                "division_id": div_id,
                "division_name": div_name,
                "teams_count": len(standings),
                "confirmed_matches": len(div_matches),
                "total_goals": div_match_goals,
                "top_scorer": top_scorer_str,
            })

        return {
            "status": "ok" if not discrepancies else "warning",
            "season_id": target_season_id,
            "divisions_checked": len(active_divisions),
            "matches_checked": total_matches_checked,
            "events_checked": total_events_checked,
            "total_goals_in_matches": total_goals_in_matches,
            "total_goals_in_events": total_goals_in_events,
            "discrepancies": discrepancies,
            "division_summaries": division_summaries,
        }


# ============================================================================
# Player Cabinet — Mini App tab «Мой Клуб»
# Repository helpers backing api/routes_player_cabinet.py. They mirror what the
# bot cabinet (handlers/cabinet.py) shows, but shaped for JSON transport.
# ============================================================================

CABINET_ACTIVE_MATCH_STATUSES = ("pending", "reported", "disputed")


def _shape_cabinet_match(row: sqlite3.Row | dict, team_name: str, telegram_id: int) -> dict:
    """Convert a raw matches row into the Mini App cabinet match payload."""
    d = dict(row)
    own = (team_name or "").strip().lower()
    is_home = (d.get("player1_team") or "").strip().lower() == own

    if is_home:
        opponent_team = d.get("player2_team")
        opponent_user = d.get("player2_username")
        my_score, opp_score = d.get("player1_score"), d.get("player2_score")
    else:
        opponent_team = d.get("player1_team")
        opponent_user = d.get("player1_username")
        my_score, opp_score = d.get("player2_score"), d.get("player1_score")

    proposed_by = d.get("proposed_by")
    has_photo = bool(d.get("photo_id"))
    # У кубковой игры round_number = -1: подписывать её надо этапом и номером игры.
    is_cup = match_is_cup(d)
    return {
        "id": d.get("id"),
        "round_number": d.get("round_number"),
        "is_cup": is_cup,
        "cup_stage": d.get("cup_stage") if is_cup else None,
        "game_num": d.get("game_num_in_series") if is_cup else None,
        "deadline": None,  # заполняется вызывающим из rounds
        "opponent_team": opponent_team,
        "opponent_user": opponent_user,
        "is_home": bool(is_home),
        "my_score": my_score,
        "opp_score": opp_score,
        "score": f"{my_score} : {opp_score}" if my_score is not None and opp_score is not None else None,
        "status": d.get("status"),
        "time_status": d.get("time_status") or "none",
        "proposed_time": d.get("proposed_time"),
        "proposed_by_me": bool(proposed_by) and int(proposed_by) == int(telegram_id),
        "has_photo": has_photo,
        "photo_id": d.get("photo_id"),
        "photo_url": f"/api/matches/{d.get('id')}/photo" if has_photo else None,
    }


def _attach_round_deadlines(matches: list[dict], division_id: int | None) -> None:
    """Fill the `deadline` field of cabinet matches from the rounds table."""
    cache: dict[int, str | None] = {}
    for m in matches:
        r_num = m.get("round_number")
        # Кубковой игре тур не принадлежит: round_number = -1 — не номер тура.
        if r_num is None or m.get("is_cup"):
            continue
        if r_num not in cache:
            info = get_round_info(r_num, division_id=division_id)
            cache[r_num] = (info or {}).get("deadline")
        m["deadline"] = cache[r_num]


def get_cabinet_matches(telegram_id: int, limit: int = 20) -> list[dict]:
    """Active (unplayed / reported / disputed) matches of a coach's club."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name, division_id FROM users WHERE telegram_id = ?", (telegram_id,))
        u_row = cursor.fetchone()
        if not u_row or not u_row["team_name"]:
            return []
        team = u_row["team_name"]
        div_id = u_row["division_id"] if "division_id" in u_row.keys() else None

        cursor.execute(
            """
            SELECT
                m.id, m.round_number, m.status, m.photo_id,
                m.tournament_type, m.cup_stage, m.game_num_in_series,
                m.player1_team, m.player2_team, m.player1_score, m.player2_score,
                m.proposed_time, m.proposed_by, COALESCE(m.time_status, 'none') AS time_status,
                u1.username AS player1_username, u2.username AS player2_username
            FROM matches m
            JOIN rounds r ON m.round_number = r.round_number
                AND COALESCE(m.division_id, 1) = COALESCE(r.division_id, 1)
                AND (m.season_id = r.season_id OR m.season_id IS NULL)
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?))
              AND m.status IN (?, ?, ?)
              AND r.is_open = 1
            ORDER BY m.round_number ASC, m.id ASC
            LIMIT ?
            """,
            (team, team, *CABINET_ACTIVE_MATCH_STATUSES, limit)
        )
        matches = [_shape_cabinet_match(row, team, telegram_id) for row in cursor.fetchall()]

    _attach_round_deadlines(matches, div_id)
    return matches


def get_cabinet_recent_matches(telegram_id: int, limit: int = 5) -> list[dict]:
    """Most recently finished (confirmed) matches of a coach's club."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT team_name, division_id FROM users WHERE telegram_id = ?", (telegram_id,))
        u_row = cursor.fetchone()
        if not u_row or not u_row["team_name"]:
            return []
        team = u_row["team_name"]
        div_id = u_row["division_id"] if "division_id" in u_row.keys() else None

        cursor.execute(
            """
            SELECT
                m.id, m.round_number, m.status, m.photo_id,
                m.tournament_type, m.cup_stage, m.game_num_in_series,
                m.player1_team, m.player2_team, m.player1_score, m.player2_score,
                m.proposed_time, m.proposed_by, COALESCE(m.time_status, 'none') AS time_status,
                u1.username AS player1_username, u2.username AS player2_username
            FROM matches m
            LEFT JOIN users u1 ON LOWER(m.player1_team) = LOWER(u1.team_name)
            LEFT JOIN users u2 ON LOWER(m.player2_team) = LOWER(u2.team_name)
            WHERE (LOWER(m.player1_team) = LOWER(?) OR LOWER(m.player2_team) = LOWER(?))
              AND m.status = 'confirmed'
            ORDER BY m.played_at DESC, m.id DESC
            LIMIT ?
            """,
            (team, team, limit)
        )
        matches = [_shape_cabinet_match(row, team, telegram_id) for row in cursor.fetchall()]

    _attach_round_deadlines(matches, div_id)
    return matches


def get_cabinet_squad_stats(team_name: str) -> dict:
    """Club roster with per-player goal/assist totals from confirmed matches.

    Отличается от `get_club_squad_stats` (её использует бот-кабинет и карточка
    клуба): здесь добавлены позиции и лидеры клуба, а ответ — словарь, а не
    список, поэтому это отдельная функция, а не замена существующей.

    Жёлтые/красные карточки в схеме не хранятся: `match_events.event_type`
    ограничен CHECK ('goal', 'assist'), а в `squad_players` карточных колонок нет.
    Поля отдаются нулями, чтобы контракт API оставался стабильным.

    Голы и ассисты считает тот же `_club_player_event_totals`, что и карточка
    клуба: сезон активный, а разные написания одного игрока ('Emegha' из OCR и
    'EMEGA' из заявки) сводятся к имени из состава. Корона сводится так же.
    """
    empty = {"players": [], "top_scorer": None, "top_assistant": None, "top_mvp": None}
    if not team_name:
        return empty

    roster = get_squad_with_positions(team_name)
    canon = resolve_team_name(team_name) or team_name.strip()
    act = get_active_season()
    season_id = act["id"] if act else 1

    raw_mvps: dict[str, int] = {}
    with transaction() as conn:
        cursor = conn.cursor()
        squad_names = [p["player_name"] for p in roster if p.get("player_name")]
        event_totals = _club_player_event_totals(cursor, canon, squad_names, season_id)

        # 👑 Награды «Игрок матча» во всех подтверждённых матчах клуба.
        # Корона могла достаться сопернику, поэтому принадлежность проверяется
        # прямо в запросе: имя должно быть либо в событиях этого клуба в том же
        # матче, либо в его заявленном составе. Имя, не подошедшее ни одному
        # клубу, не засчитывается никому — это надёжнее, чем отдать корону
        # тёзке из другой команды.
        cursor.execute(
            """
            SELECT TRIM(m.mvp_player) AS player_name, COUNT(*) AS total
            FROM matches m
            WHERE m.status = 'confirmed'
              AND (m.season_id = ? OR m.season_id IS NULL)
              AND m.mvp_player IS NOT NULL
              AND TRIM(m.mvp_player) <> ''
              AND (LOWER(TRIM(m.player1_team)) = LOWER(TRIM(?))
                   OR LOWER(TRIM(m.player2_team)) = LOWER(TRIM(?)))
              AND (EXISTS (
                       SELECT 1 FROM match_events me
                       WHERE me.match_id = m.id
                         AND LOWER(TRIM(me.team_name)) = LOWER(TRIM(?))
                         AND LOWER(TRIM(me.player_name)) = LOWER(TRIM(m.mvp_player))
                   )
                   OR EXISTS (
                       SELECT 1 FROM squad_players sp
                       WHERE LOWER(TRIM(sp.team_name)) = LOWER(TRIM(?))
                         AND LOWER(TRIM(sp.player_name)) = LOWER(TRIM(m.mvp_player))
                   ))
            GROUP BY LOWER(TRIM(m.mvp_player))
            """,
            (season_id, team_name.strip(), team_name.strip(), team_name.strip(), team_name.strip())
        )
        for row in cursor.fetchall():
            raw_mvps[row["player_name"]] = int(row["total"] or 0)

    def _key(name: str) -> str:
        return normalize_player_name_key(name) or name.strip().lower()

    # Одна строка на игрока, ключ — нормализованное имя. Порядок: заявка, затем
    # бомбардиры, распознанные OCR раньше, чем состав попал в squad_players.
    by_key: dict[str, dict] = {}

    def _row(name: str, position: str | None = None) -> dict:
        return by_key.setdefault(_key(name), {
            "player_name": name,
            "position": position,
            "goals": 0,
            "assists": 0,
            "mvp_count": 0,
            "yellow_cards": 0,
            "red_cards": 0,
        })

    for p in roster:
        if p.get("player_name"):
            _row(p["player_name"], p.get("position"))
    for t in event_totals:
        row = _row(t["player_name"])
        row["goals"] += t["goals"]
        row["assists"] += t["assists"]

    # Корона сводится к тем же строкам: сперва к имени из заявки, затем к
    # написанию из событий. Игрока без очков и вне заявки (вратарь, защитник)
    # она всё равно показывает — без своей строки его награда исчезла бы.
    known_names = [r["player_name"] for r in by_key.values()]
    for mvp_name, total in raw_mvps.items():
        name = (
            match_roster_name(mvp_name, squad_names)
            or match_roster_name(mvp_name, known_names)
            or mvp_name
        )
        _row(name)["mvp_count"] += total

    players = list(by_key.values())
    players.sort(key=lambda p: (-p["goals"], -p["assists"], p["player_name"] or ""))

    top_scorer = next((p for p in players if p["goals"] > 0), None)
    top_assistant = None
    by_assists = sorted(players, key=lambda p: (-p["assists"], p["player_name"] or ""))
    if by_assists and by_assists[0]["assists"] > 0:
        top_assistant = by_assists[0]

    top_mvp = None
    by_mvp = sorted(players, key=lambda p: (-p["mvp_count"], p["player_name"] or ""))
    if by_mvp and by_mvp[0]["mvp_count"] > 0:
        top_mvp = {"player_name": by_mvp[0]["player_name"], "mvp_count": by_mvp[0]["mvp_count"]}

    return {
        "players": players,
        "top_scorer": top_scorer,
        "top_assistant": top_assistant,
        "top_mvp": top_mvp,
    }


def save_draft(draft_uuid: str, draft_data: dict) -> None:
    """Save or update a pending match draft in SQLite."""
    with transaction() as conn:
        cursor = conn.cursor()
        data_json = json.dumps(draft_data, ensure_ascii=False)
        cursor.execute(
            "REPLACE INTO pending_drafts (draft_uuid, draft_data, created_at) VALUES (?, ?, datetime('now', '+3 hours'))",
            (draft_uuid, data_json)
        )


def get_draft(draft_uuid: str) -> dict | None:
    """Retrieve a pending match draft by uuid, or None if not found."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT draft_data FROM pending_drafts WHERE draft_uuid = ?", (draft_uuid,))
        row = cursor.fetchone()
        if not row:
            return None
        try:
            return json.loads(row["draft_data"])
        except Exception:
            logger.exception(f"Corrupt draft JSON for uuid {draft_uuid}")
            return None


def delete_draft(draft_uuid: str) -> None:
    """Delete a pending draft by uuid."""
    with transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM pending_drafts WHERE draft_uuid = ?", (draft_uuid,))

