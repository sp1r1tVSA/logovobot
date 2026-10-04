"""Миграция 033: таблицы трансферного окна.

Только аддитивно: `CREATE TABLE IF NOT EXISTS` и отметка в `schema_migrations`.
Вызывается из `database.init_db()` тем же курсором, что и остальные миграции,
поэтому живёт в той же транзакции.

Деньги — целые тысячи (`*_k`). Остаток бюджета и занятые слоты не хранятся:
они считаются при чтении из `transfers` и `transfer_slot_purchases`
(см. `transfers.engine.compute_ledger`).
"""

import sqlite3

MIGRATION_033_TRANSFER_WINDOW = "033_transfer_window"

WINDOW_STATUSES = ("draft", "open", "closed")
TRANSFER_KINDS = ("deal", "free_agent", "surcharge", "urn_sale", "urn_buy")
TRANSFER_STATUSES = (
    "pending_counterparty", "pending_manager", "approved", "rejected", "withdrawn", "cancelled",
)
TOPIC_TYPES = ("requests", "feed", "alerts", "fa")
# `transfer_topics` из миграции 033 принимает только первые три типа (CHECK, который в SQLite
# без пересоздания таблицы не изменить), поэтому остальные лежат в `transfer_topics_ext`.
EXT_TOPIC_TYPES = ("fa",)


def apply_schema(cursor: sqlite3.Cursor) -> None:
    # Одновременно не больше одного окна не в `closed`: выражение в частичном
    # индексе всегда 1, поэтому уникальность пропускает только одну строку.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_windows (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id INTEGER,
            title TEXT,
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK(status IN ('draft', 'open', 'closed')),
            created_by INTEGER,
            opened_at TIMESTAMP,
            opened_by INTEGER,
            auto_close_at TIMESTAMP,
            closed_at TIMESTAMP,
            closed_by INTEGER,
            max_buys INTEGER NOT NULL,
            max_sells INTEGER NOT NULL,
            max_extra_slots INTEGER NOT NULL,
            slot_price_coins INTEGER NOT NULL,
            ovr_cap INTEGER NOT NULL,
            min_core_players INTEGER NOT NULL,
            fa_opens_at TIMESTAMP,
            fa_forbidden_clubs TEXT NOT NULL DEFAULT '[]',
            fa_restricted_clubs TEXT NOT NULL DEFAULT '[]',
            fa_ovr_cap INTEGER NOT NULL,
            urn_divisor_sellable INTEGER NOT NULL,
            urn_divisor_unsellable INTEGER NOT NULL,
            urn_max_per_club INTEGER NOT NULL,
            urn_restricted_clubs TEXT NOT NULL DEFAULT '[]',
            surcharge_min_ovr INTEGER NOT NULL,
            surcharge_table TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            updated_at TIMESTAMP
        )
    """)
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_transfer_windows_one_active "
        "ON transfer_windows((status != 'closed')) WHERE status != 'closed'"
    )

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_club_budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES transfer_windows(id),
            club_name TEXT NOT NULL,
            budget_k INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('rule', 'manual')),
            updated_by INTEGER,
            updated_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            UNIQUE(window_id, club_name)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES transfer_windows(id),
            kind TEXT NOT NULL
                CHECK(kind IN ('deal', 'free_agent', 'surcharge', 'urn_sale', 'urn_buy')),
            player_name TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            from_club TEXT,
            to_club TEXT,
            from_user INTEGER,
            to_user INTEGER,
            price_k INTEGER NOT NULL DEFAULT 0,
            ovr INTEGER,
            tm_price_k INTEGER,
            special_price_k INTEGER,
            sellable INTEGER,
            urn_item_id INTEGER REFERENCES transfers(id),
            source_text TEXT,
            commented_at TIMESTAMP,
            reported_budget_k INTEGER,
            photo_file_id TEXT,
            initiator_id INTEGER,
            status TEXT NOT NULL
                CHECK(status IN ('pending_counterparty', 'pending_manager', 'approved',
                                 'rejected', 'withdrawn', 'cancelled')),
            warnings TEXT NOT NULL DEFAULT '[]',
            decided_by INTEGER,
            decided_at TIMESTAMP,
            decided_reason TEXT,
            squad_applied_at TIMESTAMP,
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            updated_at TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_window_status ON transfers(window_id, status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_to_club ON transfers(window_id, to_club)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_from_club ON transfers(window_id, from_club)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_norm_name ON transfers(window_id, norm_name)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfers_urn_item ON transfers(urn_item_id)")

    # Что именно трансфер поменял в squad_players — для отката.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_squad_ops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id INTEGER NOT NULL REFERENCES transfers(id),
            op TEXT NOT NULL CHECK(op IN ('add', 'remove')),
            team_name TEXT NOT NULL,
            player_name TEXT NOT NULL,
            position TEXT,
            applied_by INTEGER,
            applied_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            reverted_at TIMESTAMP
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_transfer_squad_ops_transfer ON transfer_squad_ops(transfer_id)")

    # Исходный состав клуба на момент открытия окна — основа правила «N игроков».
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_core_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES transfer_windows(id),
            club_name TEXT NOT NULL,
            player_name TEXT NOT NULL,
            norm_name TEXT NOT NULL,
            position TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            UNIQUE(window_id, club_name, norm_name)
        )
    """)

    # Справочник игроков, накапливаемый из заявок: последняя известная карта.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            norm_name TEXT NOT NULL UNIQUE,
            player_name TEXT NOT NULL,
            last_club TEXT,
            ovr INTEGER,
            price_k INTEGER,
            banned INTEGER NOT NULL DEFAULT 0,
            ban_reason TEXT,
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            updated_at TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_topics (
            topic_type TEXT PRIMARY KEY CHECK(topic_type IN ('requests', 'feed', 'alerts')),
            group_chat_id INTEGER NOT NULL,
            message_thread_id INTEGER,
            bound_by INTEGER,
            bound_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_topics_ext (
            topic_type TEXT PRIMARY KEY CHECK(topic_type IN ('fa')),
            group_chat_id INTEGER NOT NULL,
            message_thread_id INTEGER,
            bound_by INTEGER,
            bound_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
        )
    """)

    # Какие напоминания уже отправлены: (окно, метка) уникальны, поэтому повтор после рестарта невозможен.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_reminders (
            window_id INTEGER NOT NULL REFERENCES transfer_windows(id),
            tag TEXT NOT NULL,
            sent_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            PRIMARY KEY (window_id, tag)
        )
    """)

    # Обмен «игрок на игрока»: две заявки-сделки, связанные парой строк (в обе стороны).
    # Решаются вместе — подтверждение, одобрение, отклонение, отзыв и отмена идут по паре.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_swap_links (
            transfer_id INTEGER PRIMARY KEY REFERENCES transfers(id),
            partner_id INTEGER NOT NULL REFERENCES transfers(id),
            CHECK(transfer_id <> partner_id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_slot_purchases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            window_id INTEGER NOT NULL REFERENCES transfer_windows(id),
            club_name TEXT NOT NULL,
            slot_type TEXT NOT NULL CHECK(slot_type IN ('buy', 'sell')),
            price_coins INTEGER NOT NULL,
            user_id INTEGER,
            coin_tx_id INTEGER,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'refunded')),
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours'))
        )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_transfer_slot_purchases_club "
        "ON transfer_slot_purchases(window_id, club_name)"
    )

    # Лишение окна: клуб и/или тренер, по сезон `until_season_id` включительно.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transfer_sanctions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            club_name TEXT,
            user_id INTEGER,
            from_season_id INTEGER NOT NULL,
            until_season_id INTEGER NOT NULL,
            reason TEXT,
            created_by INTEGER,
            created_at TIMESTAMP NOT NULL DEFAULT (datetime('now', '+3 hours')),
            lifted_at TIMESTAMP,
            lifted_by INTEGER,
            CHECK(club_name IS NOT NULL OR user_id IS NOT NULL),
            CHECK(until_season_id >= from_season_id)
        )
    """)

    cursor.execute(
        "INSERT OR IGNORE INTO schema_migrations (version, description) VALUES (?, ?)",
        (MIGRATION_033_TRANSFER_WINDOW,
         "transfer window: windows, budgets, transfers, squad ops, core snapshot, players, topics, slots, sanctions"),
    )
