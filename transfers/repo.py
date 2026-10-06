"""SQL трансферного окна. Весь доступ к таблицам миграции 033 — только здесь.

Работает через `database.transaction()` (реентерабельный: вложенный вызов
присоединяется к внешней транзакции), запросы только параметризованные, без
подстановки имён колонок. Время — МСК (`time_utils`), и каждый INSERT явно
пишет свои временные колонки.

Клубы хранятся каноническими именами, но сравниваются через
`normalize_team_name` там, где имя пришло от человека.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping

from club_registry import resolve_team_name
from database import normalize_player_name_key, normalize_team_name, transaction
from time_utils import DT_FORMAT, now_msk_str, parse_msk
from transfers import config as tcfg
from transfers.engine import (
    ACTIVE_STATUSES,
    ClubLedger,
    WindowSettings,
    compute_ledger,
    norm_club,
    norm_player,
)
from transfers.schema import EXT_TOPIC_TYPES, TOPIC_TYPES, TRANSFER_KINDS, TRANSFER_STATUSES


class WindowConflict(Exception):
    """Уже есть незакрытое окно — второе создать нельзя."""


# Настройки окна, которые меняет ответственный. Значение — тип поля.
INT_SETTINGS = (
    "max_buys", "max_sells", "max_extra_slots", "slot_price_coins", "ovr_cap",
    "min_core_players", "fa_ovr_cap", "urn_divisor_sellable", "urn_divisor_unsellable",
    "urn_max_per_club", "surcharge_min_ovr",
)
LIST_SETTINGS = ("fa_forbidden_clubs", "fa_restricted_clubs", "urn_restricted_clubs")
TEXT_SETTINGS = ("title", "fa_opens_at", "auto_close_at")
DATETIME_SETTINGS = ("fa_opens_at", "auto_close_at")
TABLE_SETTINGS = ("surcharge_table",)
SETTINGS_KEYS = INT_SETTINGS + LIST_SETTINGS + TEXT_SETTINGS + TABLE_SETTINGS

_POSITIVE_SETTINGS = ("urn_divisor_sellable", "urn_divisor_unsellable")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


# ─── Окна ────────────────────────────────────────────────────────────────────

def create_window(season_id: int | None, created_by: int | None, title: str | None = None) -> int:
    """Создать окно в статусе `draft` с настройками по умолчанию.

    Пока есть незакрытое окно — `WindowConflict` (это держит частичный
    уникальный индекс, а не проверка в коде).
    """
    now = now_msk_str()
    table = json.dumps({str(k): v for k, v in tcfg.DEFAULT_SURCHARGE_TABLE.items()})
    try:
        with transaction() as conn:
            cur = conn.execute(
                """INSERT INTO transfer_windows (
                       season_id, title, status, created_by,
                       max_buys, max_sells, max_extra_slots, slot_price_coins, ovr_cap,
                       min_core_players, fa_ovr_cap, urn_divisor_sellable,
                       urn_divisor_unsellable, urn_max_per_club, surcharge_min_ovr,
                       surcharge_table, created_at, updated_at
                   ) VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (season_id, title, created_by,
                 tcfg.DEFAULT_MAX_BUYS, tcfg.DEFAULT_MAX_SELLS, tcfg.DEFAULT_MAX_EXTRA_SLOTS,
                 tcfg.DEFAULT_SLOT_PRICE_COINS, tcfg.DEFAULT_OVR_CAP, tcfg.DEFAULT_MIN_CORE_PLAYERS,
                 tcfg.DEFAULT_FA_OVR_CAP, tcfg.DEFAULT_URN_DIVISOR_SELLABLE,
                 tcfg.DEFAULT_URN_DIVISOR_UNSELLABLE, tcfg.DEFAULT_URN_MAX_PER_CLUB,
                 tcfg.DEFAULT_SURCHARGE_MIN_OVR, table, now, now),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError as exc:
        raise WindowConflict("an unclosed transfer window already exists") from exc


def get_window(window_id: int) -> dict | None:
    with transaction() as conn:
        return _row(conn.execute("SELECT * FROM transfer_windows WHERE id = ?", (window_id,)).fetchone())


def get_active_window() -> dict | None:
    """Незакрытое окно (`draft` или `open`) — оно максимум одно."""
    with transaction() as conn:
        return _row(conn.execute(
            "SELECT * FROM transfer_windows WHERE status != 'closed' ORDER BY id DESC LIMIT 1"
        ).fetchone())


def get_latest_window() -> dict | None:
    with transaction() as conn:
        return _row(conn.execute("SELECT * FROM transfer_windows ORDER BY id DESC LIMIT 1").fetchone())


def get_window_settings(window_id: int) -> WindowSettings | None:
    row = get_window(window_id)
    return WindowSettings.from_row(row) if row else None


def _clean_setting(key: str, value):
    if key in INT_SETTINGS:
        if isinstance(value, bool):
            raise ValueError(f"{key}: integer expected")
        number = int(value)
        if number < 0 or (key in _POSITIVE_SETTINGS and number == 0):
            raise ValueError(f"{key}: out of range")
        return number
    if key in LIST_SETTINGS:
        if isinstance(value, str):
            value = [part for part in value.split(",")]
        clubs = [str(c).strip() for c in value if str(c).strip()]
        return json.dumps(clubs, ensure_ascii=False)
    if key in TABLE_SETTINGS:
        if value is None:
            return None
        table = {}
        for ovr, price in dict(value).items():
            ovr_i, price_i = int(ovr), int(price)
            if ovr_i <= 0 or price_i < 0:
                raise ValueError(f"{key}: out of range")
            table[str(ovr_i)] = price_i
        return json.dumps(dict(sorted(table.items(), key=lambda kv: int(kv[0]))))
    # TEXT_SETTINGS
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if key in DATETIME_SETTINGS:
        # Время сравнивается строками с now_msk_str(), поэтому только формат хранения.
        moment = parse_msk(text)
        if moment is None:
            raise ValueError(f"{key}: unreadable datetime")
        return moment.strftime(DT_FORMAT)
    return text


def update_window_settings(window_id: int, changes: Mapping) -> dict:
    """Поменять настройки окна. Неизвестный ключ или плохое значение — ValueError.

    Читаем строку, сливаем изменения и пишем все поля одним статическим
    запросом: имена колонок в SQL не подставляются.
    """
    unknown = set(changes) - set(SETTINGS_KEYS)
    if unknown:
        raise ValueError(f"unknown settings: {sorted(unknown)}")
    cleaned = {key: _clean_setting(key, value) for key, value in changes.items()}
    with transaction() as conn:
        row = conn.execute("SELECT * FROM transfer_windows WHERE id = ?", (window_id,)).fetchone()
        if row is None:
            raise ValueError("window not found")
        merged = dict(row)
        merged.update(cleaned)
        conn.execute(
            """UPDATE transfer_windows SET
                   title = ?, fa_opens_at = ?, auto_close_at = ?,
                   max_buys = ?, max_sells = ?, max_extra_slots = ?, slot_price_coins = ?,
                   ovr_cap = ?, min_core_players = ?, fa_ovr_cap = ?,
                   urn_divisor_sellable = ?, urn_divisor_unsellable = ?, urn_max_per_club = ?,
                   surcharge_min_ovr = ?, fa_forbidden_clubs = ?, fa_restricted_clubs = ?,
                   urn_restricted_clubs = ?, surcharge_table = ?, updated_at = ?
               WHERE id = ?""",
            (merged["title"], merged["fa_opens_at"], merged["auto_close_at"],
             merged["max_buys"], merged["max_sells"], merged["max_extra_slots"],
             merged["slot_price_coins"], merged["ovr_cap"], merged["min_core_players"],
             merged["fa_ovr_cap"], merged["urn_divisor_sellable"], merged["urn_divisor_unsellable"],
             merged["urn_max_per_club"], merged["surcharge_min_ovr"], merged["fa_forbidden_clubs"],
             merged["fa_restricted_clubs"], merged["urn_restricted_clubs"],
             merged["surcharge_table"], now_msk_str(), window_id),
        )
        return dict(conn.execute("SELECT * FROM transfer_windows WHERE id = ?", (window_id,)).fetchone())


def open_window(window_id: int, actor_id: int | None) -> bool:
    """`draft` → `open`. False, если окно не в черновике."""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_windows SET status = 'open', opened_at = ?, opened_by = ?, updated_at = ? "
            "WHERE id = ? AND status = 'draft'",
            (now_msk_str(), actor_id, now_msk_str(), window_id),
        )
        return cur.rowcount == 1


def close_window(window_id: int, actor_id: int | None) -> bool:
    """`draft`/`open` → `closed`. False, если окно уже закрыто."""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_windows SET status = 'closed', closed_at = ?, closed_by = ?, updated_at = ? "
            "WHERE id = ? AND status != 'closed'",
            (now_msk_str(), actor_id, now_msk_str(), window_id),
        )
        return cur.rowcount == 1


# ─── Бюджеты ─────────────────────────────────────────────────────────────────

def set_club_budget(window_id: int, club_name: str, budget_k: int,
                    updated_by: int | None, source: str = "manual") -> None:
    if source not in ("rule", "manual"):
        raise ValueError("source must be 'rule' or 'manual'")
    if int(budget_k) < 0:
        raise ValueError("budget must not be negative")
    with transaction() as conn:
        existing = _find_budget_row(conn, window_id, club_name)
        if existing is not None:
            conn.execute(
                "UPDATE transfer_club_budgets SET budget_k = ?, source = ?, updated_by = ?, updated_at = ? "
                "WHERE id = ?",
                (int(budget_k), source, updated_by, now_msk_str(), existing["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO transfer_club_budgets (window_id, club_name, budget_k, source, updated_by, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (window_id, club_name.strip(), int(budget_k), source, updated_by, now_msk_str()),
            )


def _find_budget_row(conn, window_id: int, club_name: str):
    key = norm_club(club_name)
    for row in conn.execute(
        "SELECT * FROM transfer_club_budgets WHERE window_id = ?", (window_id,)
    ).fetchall():
        if norm_club(row["club_name"]) == key:
            return row
    return None


def get_club_budget(window_id: int, club_name: str) -> int | None:
    """Бюджет клуба или None, если его не задали (расчёт считает это нулём)."""
    with transaction() as conn:
        row = _find_budget_row(conn, window_id, club_name)
        return int(row["budget_k"]) if row is not None else None


def get_club_budgets(window_id: int) -> list[dict]:
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_club_budgets WHERE window_id = ? ORDER BY club_name", (window_id,)
        ).fetchall()]


# ─── Заявки ──────────────────────────────────────────────────────────────────

_TRANSFER_FIELDS = (
    "from_club", "to_club", "from_user", "to_user", "price_k", "ovr", "tm_price_k",
    "special_price_k", "sellable", "urn_item_id", "source_text", "commented_at",
    "reported_budget_k", "photo_file_id", "initiator_id",
)


def insert_transfer(window_id: int, kind: str, player_name: str, status: str,
                    warnings: Iterable[Mapping] = (), **fields) -> int:
    if kind not in TRANSFER_KINDS:
        raise ValueError(f"unknown transfer kind: {kind}")
    if status not in TRANSFER_STATUSES:
        raise ValueError(f"unknown transfer status: {status}")
    unknown = set(fields) - set(_TRANSFER_FIELDS)
    if unknown:
        raise ValueError(f"unknown transfer fields: {sorted(unknown)}")
    name = (player_name or "").strip()
    if not norm_player(name):
        raise ValueError("player name is empty")
    values = {key: fields.get(key) for key in _TRANSFER_FIELDS}
    if values["price_k"] is None:
        values["price_k"] = 0
    if values["sellable"] is not None:
        values["sellable"] = 1 if values["sellable"] else 0
    now = now_msk_str()
    with transaction() as conn:
        cur = conn.execute(
            """INSERT INTO transfers (
                   window_id, kind, player_name, norm_name, from_club, to_club, from_user, to_user,
                   price_k, ovr, tm_price_k, special_price_k, sellable, urn_item_id, source_text,
                   commented_at, reported_budget_k, photo_file_id, initiator_id, status, warnings,
                   created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (window_id, kind, name, norm_player(name),
             values["from_club"], values["to_club"], values["from_user"], values["to_user"],
             int(values["price_k"]), values["ovr"], values["tm_price_k"], values["special_price_k"],
             values["sellable"], values["urn_item_id"], values["source_text"],
             values["commented_at"], values["reported_budget_k"], values["photo_file_id"],
             values["initiator_id"], status, json.dumps(list(warnings), ensure_ascii=False),
             now, now),
        )
        return int(cur.lastrowid)


# Заявка вместе с парой по обмену: `swap_partner_id` — вторая половина обмена или NULL.
_TRANSFER_SELECT = ("SELECT t.*, l.partner_id AS swap_partner_id FROM transfers t "
                    "LEFT JOIN transfer_swap_links l ON l.transfer_id = t.id")


def get_transfer(transfer_id: int) -> dict | None:
    with transaction() as conn:
        return _row(conn.execute(_TRANSFER_SELECT + " WHERE t.id = ?", (transfer_id,)).fetchone())


def link_swap(first_id: int, second_id: int) -> None:
    """Связать две заявки в обмен (в обе стороны)."""
    if first_id == second_id:
        raise ValueError("a swap needs two different transfers")
    with transaction() as conn:
        conn.execute("INSERT INTO transfer_swap_links (transfer_id, partner_id) VALUES (?, ?)",
                     (first_id, second_id))
        conn.execute("INSERT INTO transfer_swap_links (transfer_id, partner_id) VALUES (?, ?)",
                     (second_id, first_id))


# ─── Доска «ищу / продаю» ────────────────────────────────────────────────────

# Отклики считаются по статусу их сделки: висящие и одобренные — отдельно.
_LOT_SELECT = (
    "SELECT b.*, "
    "(SELECT COUNT(*) FROM transfer_board_responses r JOIN transfers t ON t.id = r.transfer_id "
    " WHERE r.lot_id = b.id AND t.status IN ('pending_counterparty', 'pending_manager')) AS responses_pending, "
    "(SELECT COUNT(*) FROM transfer_board_responses r JOIN transfers t ON t.id = r.transfer_id "
    " WHERE r.lot_id = b.id AND t.status = 'approved') AS responses_approved "
    "FROM transfer_board_lots b"
)


def insert_lot(window_id: int, club_name: str, user_id: int, side: str, *, player_name: str | None,
               ovr: int | None, price_k: int | None, note: str | None) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO transfer_board_lots (window_id, club_name, user_id, side, player_name, norm_name, "
            "ovr, price_k, note, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (window_id, club_name, int(user_id), side, player_name,
             norm_player(player_name) if player_name else None, ovr, price_k, note, now_msk_str()),
        )
        return int(cur.lastrowid)


def get_lot(lot_id: int) -> dict | None:
    with transaction() as conn:
        return _row(conn.execute(_LOT_SELECT + " WHERE b.id = ?", (lot_id,)).fetchone())


def list_lots(window_id: int, *, status: str | None = "open") -> list[dict]:
    """Лоты окна, новые сверху. `status=None` — все."""
    sql, args = _LOT_SELECT + " WHERE b.window_id = ?", [window_id]
    if status is not None:
        sql += " AND b.status = ?"
        args.append(status)
    with transaction() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY b.id DESC", args).fetchall()]


def close_lot(lot_id: int, reason: str, closed_by: int | None) -> bool:
    """Снять открытый лот. False — он уже снят."""
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_board_lots SET status = 'closed', closed_reason = ?, closed_by = ?, closed_at = ? "
            "WHERE id = ? AND status = 'open'",
            (reason, closed_by, now_msk_str(), lot_id),
        )
        return cur.rowcount > 0


def link_board_response(transfer_id: int, lot_id: int) -> None:
    with transaction() as conn:
        conn.execute("INSERT INTO transfer_board_responses (transfer_id, lot_id) VALUES (?, ?)",
                     (transfer_id, lot_id))


def board_lot_of(transfer_id: int) -> int | None:
    """Лот, на который откликается заявка, или None."""
    with transaction() as conn:
        row = conn.execute("SELECT lot_id FROM transfer_board_responses WHERE transfer_id = ?",
                           (transfer_id,)).fetchone()
        return int(row["lot_id"]) if row else None


def get_swap_partner(transfer: dict | None) -> dict | None:
    """Вторая половина обмена или None, если заявка не из обмена."""
    pid = (transfer or {}).get("swap_partner_id")
    return get_transfer(int(pid)) if pid else None


def list_transfers(window_id: int, *, club: str | None = None,
                   statuses: Iterable[str] | None = None,
                   kinds: Iterable[str] | None = None,
                   player_name: str | None = None) -> list[dict]:
    """Заявки окна по порядку подачи. Фильтры — в Python: окно невелико."""
    with transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            _TRANSFER_SELECT + " WHERE t.window_id = ? ORDER BY t.id", (window_id,)
        ).fetchall()]
    if statuses is not None:
        allowed = set(statuses)
        rows = [r for r in rows if r["status"] in allowed]
    if kinds is not None:
        allowed_kinds = set(kinds)
        rows = [r for r in rows if r["kind"] in allowed_kinds]
    if club is not None:
        key = norm_club(club)
        rows = [r for r in rows if key and key in (norm_club(r["from_club"]), norm_club(r["to_club"]))]
    if player_name is not None:
        pkey = norm_player(player_name)
        rows = [r for r in rows if r["norm_name"] == pkey]
    return rows


def set_transfer_status(transfer_id: int, new_status: str, *, expected: Iterable[str],
                        actor_id: int | None = None, reason: str | None = None) -> bool:
    """Сменить статус, только если текущий — один из `expected`.

    Возвращает False, если заявку уже перевели (двойное нажатие, гонка двух
    админов). Окончательные статусы пишут, кто и когда решил.
    """
    if new_status not in TRANSFER_STATUSES:
        raise ValueError(f"unknown transfer status: {new_status}")
    allowed = set(expected)
    with transaction() as conn:
        row = conn.execute("SELECT status FROM transfers WHERE id = ?", (transfer_id,)).fetchone()
        if row is None or row["status"] not in allowed:
            return False
        now = now_msk_str()
        if new_status in ("approved", "rejected", "cancelled"):
            conn.execute(
                "UPDATE transfers SET status = ?, decided_by = ?, decided_at = ?, decided_reason = ?, "
                "updated_at = ? WHERE id = ? AND status = ?",
                (new_status, actor_id, now, reason, now, transfer_id, row["status"]),
            )
        else:
            conn.execute(
                "UPDATE transfers SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (new_status, now, transfer_id, row["status"]),
            )
        return True


def set_transfer_warnings(transfer_id: int, warnings: Iterable[Mapping]) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE transfers SET warnings = ?, updated_at = ? WHERE id = ?",
            (json.dumps(list(warnings), ensure_ascii=False), now_msk_str(), transfer_id),
        )


def set_transfer_photo(transfer_id: int, file_id: str | None) -> None:
    """`file_id` фото заявки — Telegram его выдаёт, когда карточка уже отправлена."""
    with transaction() as conn:
        conn.execute(
            "UPDATE transfers SET photo_file_id = ?, updated_at = ? WHERE id = ?",
            (file_id, now_msk_str(), transfer_id),
        )


def mark_squad_applied(transfer_id: int) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE transfers SET squad_applied_at = ?, updated_at = ? WHERE id = ?",
            (now_msk_str(), now_msk_str(), transfer_id),
        )


def clear_squad_applied(transfer_id: int) -> None:
    """Состав по заявке откатили — она снова «не применена»."""
    with transaction() as conn:
        conn.execute(
            "UPDATE transfers SET squad_applied_at = NULL, updated_at = ? WHERE id = ?",
            (now_msk_str(), transfer_id),
        )


def get_club_ledger(window_id: int, club_name: str,
                    exclude_transfer_id: int | None = None) -> ClubLedger:
    """Бюджет и слоты клуба по данным окна — одна точка для бота и Mini App."""
    settings = get_window_settings(window_id)
    if settings is None:
        raise ValueError("window not found")
    with transaction():
        budget = get_club_budget(window_id, club_name)
        transfers = list_transfers(window_id, statuses=ACTIVE_STATUSES)
        purchases = list_slot_purchases(window_id)
    return compute_ledger(club_name, budget, transfers, settings,
                          slot_purchases=purchases, exclude_transfer_id=exclude_transfer_id)


# ─── Доп. слоты ──────────────────────────────────────────────────────────────

def add_slot_purchase(window_id: int, club_name: str, slot_type: str, price_coins: int,
                      user_id: int | None, coin_tx_id: int | None = None) -> int:
    if slot_type not in ("buy", "sell"):
        raise ValueError("slot_type must be 'buy' or 'sell'")
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO transfer_slot_purchases "
            "(window_id, club_name, slot_type, price_coins, user_id, coin_tx_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'active', ?)",
            (window_id, club_name, slot_type, int(price_coins), user_id, coin_tx_id, now_msk_str()),
        )
        return int(cur.lastrowid)


def list_slot_purchases(window_id: int, club_name: str | None = None) -> list[dict]:
    with transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_slot_purchases WHERE window_id = ? ORDER BY id", (window_id,)
        ).fetchall()]
    if club_name is not None:
        key = norm_club(club_name)
        rows = [r for r in rows if norm_club(r["club_name"]) == key]
    return rows


def get_slot_purchase(purchase_id: int) -> dict | None:
    with transaction() as conn:
        row = conn.execute("SELECT * FROM transfer_slot_purchases WHERE id = ?", (purchase_id,)).fetchone()
    return dict(row) if row else None


def refund_slot_purchase(purchase_id: int) -> bool:
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_slot_purchases SET status = 'refunded' WHERE id = ? AND status = 'active'",
            (purchase_id,),
        )
        return cur.rowcount == 1


# ─── Санкции ─────────────────────────────────────────────────────────────────

def add_sanction(*, club_name: str | None, user_id: int | None, from_season_id: int,
                 until_season_id: int, reason: str | None, created_by: int | None) -> int:
    if not (club_name or user_id):
        raise ValueError("sanction needs a club or a user")
    if until_season_id < from_season_id:
        raise ValueError("until_season_id must not precede from_season_id")
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO transfer_sanctions "
            "(club_name, user_id, from_season_id, until_season_id, reason, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (club_name, user_id, int(from_season_id), int(until_season_id), reason, created_by,
             now_msk_str()),
        )
        return int(cur.lastrowid)


def lift_sanction(sanction_id: int, lifted_by: int | None) -> bool:
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_sanctions SET lifted_at = ?, lifted_by = ? WHERE id = ? AND lifted_at IS NULL",
            (now_msk_str(), lifted_by, sanction_id),
        )
        return cur.rowcount == 1


def list_active_sanctions(season_id: int) -> list[dict]:
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_sanctions WHERE lifted_at IS NULL "
            "AND from_season_id <= ? AND until_season_id >= ? ORDER BY id",
            (int(season_id), int(season_id)),
        ).fetchall()]


def is_sanctioned(season_id: int, club_name: str | None = None, user_id: int | None = None) -> bool:
    key = norm_club(club_name)
    for s in list_active_sanctions(season_id):
        if user_id is not None and s["user_id"] == user_id:
            return True
        if key and s["club_name"] and norm_club(s["club_name"]) == key:
            return True
    return False


def get_sanction(sanction_id: int) -> dict | None:
    with transaction() as conn:
        return _row(conn.execute("SELECT * FROM transfer_sanctions WHERE id = ?", (int(sanction_id),)).fetchone())


def list_sanctions(*, limit: int = 50) -> list[dict]:
    """Все санкции, свежие сверху — и действующие, и снятые."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_sanctions ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def coaches_of_club(club_name: str) -> list[dict]:
    """Тренеры клуба: `telegram_id`, `username`. Клуб в `users.team_name` один на тренера."""
    key = norm_club(club_name)
    if not key:
        return []
    with transaction() as conn:
        rows = conn.execute(
            "SELECT telegram_id, username, team_name FROM users WHERE team_name IS NOT NULL AND TRIM(team_name) != ''"
        ).fetchall()
    return [{"telegram_id": r["telegram_id"], "username": r["username"]} for r in rows
            if norm_club(resolve_team_name(r["team_name"]) or r["team_name"]) == key]


def list_coaches() -> list[dict]:
    """Все тренеры с клубом: `telegram_id`, `team_name` как в `users` (резолв — у вызывающего)."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT telegram_id, team_name FROM users WHERE team_name IS NOT NULL AND TRIM(team_name) != ''"
        ).fetchall()
    return [{"telegram_id": r["telegram_id"], "team_name": r["team_name"]} for r in rows]


def claim_reminder(window_id: int, tag: str) -> bool:
    """Отметить напоминание отправленным. False — оно уже было, повторять не надо."""
    with transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO transfer_reminders (window_id, tag, sent_at) VALUES (?, ?, ?)",
            (window_id, tag, now_msk_str()),
        )
        return cur.rowcount > 0


def season_names(season_ids: Iterable[int]) -> dict[int, str]:
    """{id сезона: название} для подписей санкций; неизвестный id в словарь не попадает."""
    wanted = {int(i) for i in season_ids if i}
    if not wanted:
        return {}
    with transaction() as conn:
        rows = conn.execute("SELECT id, name FROM seasons").fetchall()
    return {int(r["id"]): (r["name"] or "").strip() for r in rows if int(r["id"]) in wanted}


def list_windows(limit: int = 20) -> list[dict]:
    """Окна, свежие сверху."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_windows ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


# ─── Справочник игроков ──────────────────────────────────────────────────────

def get_player(player_name: str) -> dict | None:
    key = norm_player(player_name)
    if not key:
        return None
    with transaction() as conn:
        return _row(conn.execute("SELECT * FROM transfer_players WHERE norm_name = ?", (key,)).fetchone())


def list_pool_players() -> list[dict]:
    """Игроки справочника `transfer_players` — для автоподбора имён."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT player_name, last_club FROM transfer_players ORDER BY id").fetchall()]


def replace_player_cards(cards: Iterable[Mapping]) -> int:
    """Заменить снимок карточек Renderz целиком (одной транзакцией). Возвращает число записанных строк.

    Каждая карточка: `renderz_id`, `player_name`, `ovr` и необязательные `club`, `position`,
    `program`, `tradable`, `selected`. Пустой набор не стирает таблицу.
    """
    rows = []
    now = now_msk_str()
    for c in cards:
        key = norm_player(c.get("player_name"))
        if not key or c.get("renderz_id") is None or c.get("ovr") is None:
            continue
        rows.append((int(c["renderz_id"]), key, str(c["player_name"]).strip(), c.get("club"), int(c["ovr"]),
                     c.get("position"), c.get("program"),
                     1 if c.get("tradable", True) else 0, 1 if c.get("selected", True) else 0, now))
    if not rows:
        return 0
    with transaction() as conn:
        conn.execute("DELETE FROM transfer_player_cards")
        conn.executemany(
            "INSERT INTO transfer_player_cards "
            "(renderz_id, norm_name, player_name, club, ovr, position, program, tradable, selected, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def list_player_cards(selected_only: bool = True) -> list[dict]:
    """Карточки Renderz: по умолчанию только выбранные версии (без дублей продаваемая/непродаваемая)."""
    sql = ("SELECT renderz_id, norm_name, player_name, club, ovr, position, program, tradable, selected "
           "FROM transfer_player_cards")
    if selected_only:
        sql += " WHERE selected = 1"
    with transaction() as conn:
        return [dict(r) for r in conn.execute(sql + " ORDER BY ovr DESC, renderz_id").fetchall()]


def card_player_names(club: str) -> list[str]:
    """Полные имена игроков клуба, у которых есть карточка Renderz."""
    with transaction() as conn:
        return [r["player_name"] for r in conn.execute(
            "SELECT DISTINCT player_name FROM transfer_player_cards WHERE club = ?", (club,)).fetchall()]


def list_catalog_players() -> list[dict]:
    """Справочник игроков целиком (с OVR, ценой и баном) — для рынка."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT norm_name, player_name, last_club, ovr, price_k, banned, ban_reason "
            "FROM transfer_players ORDER BY id").fetchall()]


def upsert_player(player_name: str, *, last_club: str | None = None, ovr: int | None = None,
                  price_k: int | None = None) -> None:
    """Запомнить последнюю известную карту игрока. None не затирает прежнее."""
    key = norm_player(player_name)
    if not key:
        raise ValueError("player name is empty")
    now = now_msk_str()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM transfer_players WHERE norm_name = ?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO transfer_players "
                "(norm_name, player_name, last_club, ovr, price_k, banned, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (key, player_name.strip(), last_club, ovr, price_k, now, now),
            )
            return
        conn.execute(
            "UPDATE transfer_players SET player_name = ?, last_club = ?, ovr = ?, price_k = ?, "
            "updated_at = ? WHERE id = ?",
            (player_name.strip(),
             last_club if last_club is not None else row["last_club"],
             ovr if ovr is not None else row["ovr"],
             price_k if price_k is not None else row["price_k"],
             now, row["id"]),
        )


def set_player_ban(player_name: str, banned: bool, reason: str | None = None) -> None:
    key = norm_player(player_name)
    if not key:
        raise ValueError("player name is empty")
    now = now_msk_str()
    with transaction() as conn:
        row = conn.execute("SELECT id FROM transfer_players WHERE norm_name = ?", (key,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO transfer_players "
                "(norm_name, player_name, banned, ban_reason, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, player_name.strip(), 1 if banned else 0, reason if banned else None, now, now),
            )
        else:
            conn.execute(
                "UPDATE transfer_players SET banned = ?, ban_reason = ?, updated_at = ? WHERE id = ?",
                (1 if banned else 0, reason if banned else None, now, row["id"]),
            )


# ─── Топики ──────────────────────────────────────────────────────────────────

def bind_topic(topic_type: str, group_chat_id: int, message_thread_id: int | None,
               bound_by: int | None) -> None:
    if topic_type not in TOPIC_TYPES:
        raise ValueError(f"unknown topic type: {topic_type}")
    args = (topic_type, int(group_chat_id), message_thread_id, bound_by, now_msk_str())
    with transaction() as conn:
        if topic_type in EXT_TOPIC_TYPES:
            conn.execute(
                "INSERT INTO transfer_topics_ext (topic_type, group_chat_id, message_thread_id, bound_by, bound_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(topic_type) DO UPDATE SET group_chat_id = excluded.group_chat_id, "
                "message_thread_id = excluded.message_thread_id, bound_by = excluded.bound_by, "
                "bound_at = excluded.bound_at", args)
            return
        conn.execute(
            "INSERT INTO transfer_topics (topic_type, group_chat_id, message_thread_id, bound_by, bound_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(topic_type) DO UPDATE SET group_chat_id = excluded.group_chat_id, "
            "message_thread_id = excluded.message_thread_id, bound_by = excluded.bound_by, "
            "bound_at = excluded.bound_at", args)


def get_topic(topic_type: str) -> dict | None:
    with transaction() as conn:
        if topic_type in EXT_TOPIC_TYPES:
            return _row(conn.execute(
                "SELECT * FROM transfer_topics_ext WHERE topic_type = ?", (topic_type,)
            ).fetchone())
        return _row(conn.execute(
            "SELECT * FROM transfer_topics WHERE topic_type = ?", (topic_type,)
        ).fetchone())


def get_topics() -> dict[str, dict]:
    with transaction() as conn:
        rows = conn.execute("SELECT * FROM transfer_topics").fetchall()
        rows += conn.execute("SELECT * FROM transfer_topics_ext").fetchall()
        return {r["topic_type"]: dict(r) for r in rows}


# ─── Исходный состав ─────────────────────────────────────────────────────────

def save_core_snapshot(window_id: int, club_name: str,
                       players: Iterable[tuple[str, str | None]]) -> int:
    """Записать состав клуба на открытие окна. Повтор не дублирует игроков."""
    now = now_msk_str()
    added = 0
    with transaction() as conn:
        for name, position in players:
            key = norm_player(name)
            if not key:
                continue
            cur = conn.execute(
                "INSERT OR IGNORE INTO transfer_core_snapshot "
                "(window_id, club_name, player_name, norm_name, position, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (window_id, club_name, name.strip(), key, position, now),
            )
            added += cur.rowcount
    return added


def get_core_snapshot(window_id: int, club_name: str) -> list[dict]:
    key = norm_club(club_name)
    with transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM transfer_core_snapshot WHERE window_id = ? ORDER BY id", (window_id,)
        ).fetchall()
    return [dict(r) for r in rows if norm_club(r["club_name"]) == key]


def core_snapshot_clubs(window_id: int) -> set[str]:
    """Клубы (нормализованные имена), у которых снимок уже есть."""
    with transaction() as conn:
        rows = conn.execute(
            "SELECT DISTINCT club_name FROM transfer_core_snapshot WHERE window_id = ?", (window_id,)
        ).fetchall()
    return {norm_club(r["club_name"]) for r in rows}


def list_squad_players() -> list[dict]:
    """Все строки `squad_players` как есть — источник снимка состава.

    Позиции читаются без «самолечения» `database.get_squad_with_positions`:
    там пустая позиция уходит искать себя в интернет, а снимку она не нужна.
    """
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT team_name, player_name, position FROM squad_players ORDER BY id"
        ).fetchall()]


def has_core_snapshot(window_id: int) -> bool:
    with transaction() as conn:
        return conn.execute(
            "SELECT 1 FROM transfer_core_snapshot WHERE window_id = ? LIMIT 1", (window_id,)
        ).fetchone() is not None


# ─── Изменения состава ───────────────────────────────────────────────────────

def squad_rows(club: str) -> list[dict]:
    """Строки `squad_players` клуба (с id): клуб узнаётся через реестр, как в снимке состава."""
    key = norm_club(club)
    with transaction() as conn:
        rows = conn.execute(
            "SELECT id, team_name, player_name, position FROM squad_players ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows
            if norm_club(resolve_team_name(r["team_name"]) or r["team_name"]) == key]


def squad_delete(row_id: int) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM squad_players WHERE id = ?", (row_id,))


def squad_insert(club: str, player_name: str, position: str | None) -> str | None:
    """Добавить игрока в состав клуба. Возвращает `team_name` строки или None, если он там уже есть.

    Имя клуба берётся из его же строк: `squad_players` уникален по `team_name` + игроку, а
    регистр и написание у старых записей могут отличаться от канонического.
    """
    existing = squad_rows(club)
    team = existing[0]["team_name"] if existing else club.strip()
    with transaction() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO squad_players (team_name, player_name, position, norm_name, norm_team_name) "
            "VALUES (?, ?, ?, ?, ?)",
            (team, player_name.strip(), position, normalize_player_name_key(player_name),
             normalize_team_name(resolve_team_name(team) or team)),
        )
        return team if cur.rowcount else None


def later_squad_ops(op_id: int, transfer_id: int) -> list[dict]:
    """Не откаченные изменения состава других заявок, сделанные после `op_id`."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_squad_ops WHERE id > ? AND transfer_id != ? AND reverted_at IS NULL "
            "ORDER BY id", (op_id, transfer_id)).fetchall()]


def insert_squad_op(transfer_id: int, op: str, team_name: str, player_name: str,
                    position: str | None, applied_by: int | None) -> int:
    if op not in ("add", "remove"):
        raise ValueError("op must be 'add' or 'remove'")
    with transaction() as conn:
        cur = conn.execute(
            "INSERT INTO transfer_squad_ops "
            "(transfer_id, op, team_name, player_name, position, applied_by, applied_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (transfer_id, op, team_name, player_name, position, applied_by, now_msk_str()),
        )
        return int(cur.lastrowid)


def list_squad_ops(transfer_id: int, *, include_reverted: bool = False) -> list[dict]:
    with transaction() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM transfer_squad_ops WHERE transfer_id = ? ORDER BY id", (transfer_id,)
        ).fetchall()]
    return rows if include_reverted else [r for r in rows if r["reverted_at"] is None]


def list_window_squad_ops(window_id: int) -> list[dict]:
    """Не откаченные изменения состава по всем заявкам окна, по порядку."""
    with transaction() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT o.* FROM transfer_squad_ops o JOIN transfers t ON t.id = o.transfer_id "
            "WHERE t.window_id = ? AND o.reverted_at IS NULL ORDER BY o.id",
            (window_id,),
        ).fetchall()]


def mark_squad_ops_reverted(transfer_id: int) -> int:
    with transaction() as conn:
        cur = conn.execute(
            "UPDATE transfer_squad_ops SET reverted_at = ? WHERE transfer_id = ? AND reverted_at IS NULL",
            (now_msk_str(), transfer_id),
        )
        return cur.rowcount
