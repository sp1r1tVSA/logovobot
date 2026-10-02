"""Журнал действий админов: запись из хендлеров и каталог действий для /audit.

Пишет через `database.log_admin_action` в `admin_audit_log` (или в
`bet_audit_log`, если у админа нет строки в users — см. там). `record` не
бросает исключений: журнал не должен ломать само действие.

`ACTIONS` — известные действия: категория для фильтра и подпись по-русски.
Неизвестное действие показывается как есть в категории «Прочее», так что
забытая строка каталога ничего не прячет.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re

import database

logger = logging.getLogger(__name__)

CATEGORIES: dict[str, str] = {
    "matches": "⚽ Матчи",
    "discipline": "🟨 Дисциплина",
    "rounds": "📅 Туры",
    "clubs": "👥 Игроки и клубы",
    "cups": "🏆 Кубки",
    "admins": "🛡 Дивизионы и админы",
    "bets": "🎰 Ставки",
    "seasons": "🗓 Сезоны",
    "service": "🛠 Сервис",
    "transfers": "🔁 Трансферы",
    "other": "📌 Прочее",
}

ACTIONS: dict[str, tuple[str, str]] = {
    # Матчи
    "set_match_score": ("matches", "Внесён счёт"),
    "correct_match_score": ("matches", "Исправлен счёт"),
    "match_result_correction": ("matches", "Исправлен результат (панель)"),
    "technical_verdict": ("matches", "Техрезультат"),
    "match_reset": ("matches", "Сброшен результат"),
    "match_freeze_toggled": ("matches", "Заморозка матча"),
    "match_deadline_extended": ("matches", "Продлён срок матча"),
    "draft_confirmed": ("matches", "Подтверждён черновик"),
    "draft_rejected": ("matches", "Отклонён черновик"),
    "generate_round_robin": ("matches", "Сгенерировано расписание"),
    "result_correction": ("matches", "Исправлен результат (live)"),
    # Дисциплина
    "warn_added": ("discipline", "Выдан варн"),
    "warn_removed": ("discipline", "Снят варн"),
    "warn_amnesty": ("discipline", "Амнистия"),
    "warns_reset_user": ("discipline", "Сброшены варны игрока"),
    "season_warns_reset": ("discipline", "Сброшены варны сезона"),
    "warns_debts_reset_all": ("discipline", "Сброшены все варны и долги"),
    # Туры
    "round_opened": ("rounds", "Открыт тур / новый дедлайн"),
    "rounds_opened_batch": ("rounds", "Открыты туры пачкой"),
    "round_closed": ("rounds", "Закрыт тур"),
    # Игроки и клубы
    "player_added": ("clubs", "Добавлен игрок"),
    "club_changed": ("clubs", "Изменён клуб"),
    "club_bound": ("clubs", "Привязан клуб"),
    "club_freed": ("clubs", "Освобождён клуб"),
    "player_division_changed": ("clubs", "Сменён дивизион игрока"),
    "player_removed": ("clubs", "Игрок удалён из лиги"),
    "player_wiped": ("clubs", "Игрок стёрт полностью"),
    "role_changed": ("clubs", "Сменена роль"),
    # Кубки
    "cup_stage_bets_opened": ("cups", "Открыт приём ставок на стадию"),
    "cup_stage_started": ("cups", "Стадия кубка запущена"),
    "cup_game_winner_set": ("cups", "Назначен проход в серии"),
    # Дивизионы и админы
    "division_created": ("admins", "Создан дивизион"),
    "division_renamed": ("admins", "Переименован дивизион"),
    "division_toggled": ("admins", "Дивизион вкл/выкл"),
    "division_admin_added": ("admins", "Назначен админ дивизиона"),
    "division_admin_removed": ("admins", "Снят админ дивизиона"),
    # Ставки
    "bet_voided": ("bets", "Аннулирована ставка"),
    "market_void_bet_refund": ("bets", "Возврат по рынку"),
    "odds_changed": ("bets", "Изменён коэффициент"),
    "limit_set": ("bets", "Установлен лимит"),
    "limit_reset": ("bets", "Сброшен лимит"),
    "betting_paused": ("bets", "Ставки на паузе"),
    "betting_resumed": ("bets", "Ставки возобновлены"),
    "player_betting_banned": ("bets", "Игроку запрещены ставки"),
    "player_betting_unbanned": ("bets", "Игроку разрешены ставки"),
    "suspend_market": ("bets", "Рынок приостановлен"),
    "unsuspend_market": ("bets", "Рынок возобновлён"),
    "force_close_markets": ("bets", "Рынки закрыты принудительно"),
    "resume_match_markets": ("bets", "Рынки матча возобновлены"),
    "live_market_close": ("bets", "Live: рынок закрыт"),
    "live_market_resume": ("bets", "Live: рынок возобновлён"),
    "live_market_suspend": ("bets", "Live: рынок приостановлен"),
    "live_market_void": ("bets", "Live: рынок аннулирован"),
    "integrity_case_status": ("bets", "Статус дела о договорняке"),
    "market_open": ("bets", "Рынок открыт"),
    "market_suspended": ("bets", "Рынок приостановлен"),
    "market_closed": ("bets", "Рынок закрыт"),
    "market_settled": ("bets", "Рынок рассчитан"),
    "market_void": ("bets", "Рынок аннулирован"),
    "market_voided": ("bets", "Рынок аннулирован"),
    "market_suspend_reason": ("bets", "Причина приостановки рынка"),
    "market_resume_reason": ("bets", "Причина возобновления рынка"),
    "market_close_reason": ("bets", "Причина закрытия рынка"),
    "wallet_admin_credit": ("bets", "Начислены монеты"),
    "wallet_admin_debit": ("bets", "Списаны монеты"),
    # Сезоны
    "create_season": ("seasons", "Создан сезон"),
    "activate_season": ("seasons", "Активирован сезон"),
    "finish_season": ("seasons", "Завершён сезон"),
    "finalize_season": ("seasons", "Сезон подведён"),
    "archive_season": ("seasons", "Сезон в архиве"),
    "clear_entire_league": ("seasons", "Очищена вся лига"),
    # Сервис
    "db_backup_created": ("service", "Сделан бэкап базы"),
    "db_backup_downloaded": ("service", "Скачан бэкап базы"),
    # Трансферное окно
    "transfer_window_created": ("transfers", "Создано трансферное окно"),
    "transfer_window_opened": ("transfers", "Открыто трансферное окно"),
    "transfer_window_closed": ("transfers", "Закрыто трансферное окно"),
    "transfer_window_settings": ("transfers", "Изменены настройки окна"),
    "transfer_budget_set": ("transfers", "Задан бюджет клуба"),
    "transfer_budgets_applied": ("transfers", "Бюджеты выданы по правилам"),
    "transfer_topic_bound": ("transfers", "Привязана тема ТО"),
    "transfer_core_snapshot": ("transfers", "Дописан снимок составов"),
}

# Записи с реальным actor_id, которые засоряли бы журнал админов: пересчёт
# линии пишет odds_changed на каждый исход, трекер и live-автомат — переходы
# матча от имени игрока. «Все» их не показывает; категория «Ставки» — тоже.
NOISY_ACTIONS: tuple[str, ...] = (
    "odds_changed",
    "match_status_transition",
    "rule_market_suspension",
    "tracker_session_start",
    "tracker_session_finish",
    # Дубль match_result_correction: routes_admin_live пишет правку в обе таблицы.
    "result_correction",
)


def action_label(action: str) -> str:
    return ACTIONS.get(action, ("other", action))[1]


def action_category(action: str) -> str:
    return ACTIONS.get(action, ("other", action))[0]


def actions_in(category: str) -> list[str]:
    return [a for a, (cat, _) in ACTIONS.items() if cat == category and a not in NOISY_ACTIONS]


def journal_filter(category: str | None) -> dict:
    """Keyword arguments for `database.get_admin_journal` for a category.

    None is «все» without the noise; «other» is everything the catalog does not
    know, so an uncatalogued action is still reachable.
    """
    if not category or category not in CATEGORIES:
        return {"exclude_actions": list(NOISY_ACTIONS)}
    if category == "other":
        return {"exclude_actions": [*ACTIONS.keys(), *NOISY_ACTIONS]}
    return {"actions": actions_in(category)}


def _short(value, limit: int = 120) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


# Объект действия: (единственное, множественное). Пустая строка — не показывать:
# у бэкапа и лиги нет номера, а у лимита и паузы ставок номер — это область,
# которая и так видна в значениях и в «див.».
TARGETS: dict[str, tuple[str, str]] = {
    "match": ("матч", "матчи"),
    "market": ("рынок", "рынки"),
    "selection": ("исход", "исходы"),
    "bet": ("купон", "купоны"),
    "user": ("игрок", "игроки"),
    "round": ("тур", "туры"),
    "division": ("дивизион", "дивизионы"),
    "season": ("сезон", "сезоны"),
    "cup_stage": ("стадия кубка", "стадии кубка"),
    "integrity_case": ("дело", "дела"),
    "backup": ("", ""),
    "league": ("", ""),
    "risk_limit": ("", ""),
    "betting": ("", ""),
}

_KEYS: dict[str, str] = {
    "status": "статус",
    "odds_value": "коэф.",
    "amount": "сумма",
    "refund": "возврат",
    "balance": "баланс",
    "reason": "причина",
    "market_id": "рынок",
    "scope_type": "область",
    "limit_key": "лимит",
    "value": "значение",
    "player1_score": "голы 1",
    "player2_score": "голы 2",
    "paused": "пауза",
}

_STATUSES: dict[str, str] = {
    # рынки
    "open": "открыт",
    "suspended": "приостановлен",
    "closed": "закрыт",
    "settled": "рассчитан",
    "voided": "аннулирован",
    # матчи
    "scheduled": "запланирован",
    "pending": "не сыгран",
    "reported": "ждёт подтверждения",
    "disputed": "спорный",
    "confirmed": "подтверждён",
    "completed": "сыгран",
    "technical": "техрезультат",
    "live": "идёт",
    "finished": "завершён",
    # исходы
    "active": "активен",
    "locked": "заблокирован",
}

# Купон — это ставка, поэтому его статусы в женском роде.
_BET_STATUSES: dict[str, str] = {
    "pending": "в игре",
    "won": "выиграла",
    "lost": "проиграла",
    "refunded": "возвращена",
    "cancelled": "отменена",
    "cashed_out": "выкуплена",
}

_SCOPES: dict[str, str] = {"global": "вся лига", "division": "дивизион", "user": "игрок"}

_LIMITS: dict[str, str] = {
    "max_bet": "макс. ставка",
    "max_payout": "макс. выплата",
    "max_open_bets": "макс. открытых купонов",
    "max_daily_stake": "макс. ставок за день",
    "max_daily_loss": "макс. проигрыш за день",
    "max_open_exposure": "макс. открытый риск",
    "market_exposure_limit": "лимит риска рынка",
    "division_exposure_limit": "лимит риска дивизиона",
    "max_express_events": "макс. событий в экспрессе",
    "express_margin_pct": "надбавка на экспресс, %",
    "initial_balance": "стартовый баланс",
}

_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def _dates(text: str) -> str:
    """'2026-10-02 02:25:44' → '02.10 02:25' wherever it occurs in the text."""
    return _DATETIME.sub(lambda m: f"{m[3]}.{m[2]} {m[4]}:{m[5]}", text)


def _parse(value):
    """A JSON object stored by the betting audit, or the value as it is."""
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _scalar(key: str, value, target_type: str) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if key == "status" and isinstance(value, str):
        table = _BET_STATUSES if target_type == "bet" else _STATUSES
        return table.get(value, value)
    if key == "scope_type":
        return _SCOPES.get(str(value), str(value))
    if key == "limit_key":
        text = str(value)
        if text.startswith("ban_"):
            return f"запрет «{text[4:]}»"
        return _LIMITS.get(text, text)
    if key == "market_id":
        return f"#{value}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return _dates(str(value))


def _changes(old, new, target_type: str) -> str:
    """old/new as one line. A status change reads «закрыт → аннулирован», a
    dict reads «ключ: было → стало · ключ: значение»."""
    old, new = _parse(old), _parse(new)
    empty = (None, "")
    if isinstance(old, dict) or isinstance(new, dict):
        o = old if isinstance(old, dict) else {}
        n = new if isinstance(new, dict) else {}
        if set(o) | set(n) == {"status"}:
            return (f"{_scalar('status', o.get('status'), target_type)} → "
                    f"{_scalar('status', n.get('status'), target_type)}")
        parts = []
        for key in list(o) + [k for k in n if k not in o]:
            label = _KEYS.get(key, key)
            if key in o and key in n and o[key] != n[key]:
                parts.append(f"{label}: {_scalar(key, o[key], target_type)} → "
                             f"{_scalar(key, n[key], target_type)}")
            else:
                parts.append(f"{label}: {_scalar(key, n[key] if key in n else o[key], target_type)}")
        text = " · ".join(parts)
        return f"было: {text}" if o and not n else text
    if old not in empty and new not in empty:
        return f"{_dates(str(old))} → {_dates(str(new))}"
    if new not in empty:
        return _dates(str(new))
    if old not in empty:
        return f"было: {_dates(str(old))}"
    return ""


def _ids(ids: list) -> str:
    """#5, #7 — or #10013–#10018 for a run of consecutive numbers."""
    nums = sorted({int(i) for i in ids if str(i).lstrip("-").isdigit()})
    if not nums:
        return ""
    if len(nums) > 2 and nums[-1] - nums[0] == len(nums) - 1:
        return f"#{nums[0]}–#{nums[-1]}"
    shown = ", ".join(f"#{n}" for n in nums[:8])
    return shown + (f" и ещё {len(nums) - 8}" if len(nums) > 8 else "")


_BATCH_KEYS = ("actor_id", "action", "target_type", "division_id", "old_value", "new_value", "reason")


def _same_batch(a: dict, b: dict) -> bool:
    return (
        bool(a.get("target_id")) and bool(b.get("target_id"))
        and all(a.get(k) == b.get(k) for k in _BATCH_KEYS)
        and str(a.get("created_at") or "")[:16] == str(b.get("created_at") or "")[:16]
    )


def group_entries(rows: list[dict]) -> list[dict]:
    """Merge neighbouring rows that are one action applied to many objects in
    the same minute (six markets voided at once) into one row carrying
    `target_ids` and `count`."""
    out: list[dict] = []
    for row in rows:
        if out and _same_batch(out[-1], row):
            out[-1]["target_ids"].append(row["target_id"])
            out[-1]["count"] += 1
            continue
        out.append({**row, "target_ids": [row.get("target_id")], "count": 1})
    return out


def format_entry(row: dict) -> str:
    """One journal row (or a group from `group_entries`) as HTML: when and who
    on the first line, what and on what on the second, then old → new and the
    reason."""
    esc = html.escape
    stamp = str(row.get("created_at") or "")
    # 'YYYY-MM-DD HH:MM:SS' (MSK) → 'DD.MM HH:MM'
    when = f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]}" if len(stamp) >= 16 else stamp
    username = row.get("actor_username")
    who = f"@{esc(username)}" if username else f"ID <code>{row.get('actor_id')}</code>"
    action = str(row.get("action") or "?")
    what = f"<b>{esc(action_label(action))}</b>" if action in ACTIONS else f"<code>{esc(action)}</code>"
    count = int(row.get("count") or 1)
    if count > 1:
        what += f" ×{count}"

    details = []
    target_type = str(row.get("target_type") or "")
    if target_type:
        one, many = TARGETS.get(target_type, (target_type, target_type))
        ids = [i for i in (row.get("target_ids") or [row.get("target_id")]) if i]
        if one and ids:
            details.append(f"{many if len(ids) > 1 else one} {_ids(ids)}")
        elif one:
            details.append(one)
    if row.get("division_id"):
        details.append(f"див. {row['division_id']}")
    if details:
        what += " · " + esc(" · ".join(details))

    lines = [f"🕑 {esc(when)} · {who}", what]
    change = _changes(row.get("old_value"), row.get("new_value"), target_type)
    if change:
        lines.append(f"   {esc(_short(change, 200))}")
    if row.get("reason"):
        lines.append(f"   💬 {esc(_short(_dates(str(row['reason']))))}")
    return "\n".join(lines)


def _text(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if len(text) <= 500 else text[:497] + "…"


def record_sync(
    actor_id: int | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    *,
    old=None,
    new=None,
    division_id: int | None = None,
    reason: str | None = None,
) -> None:
    if not actor_id:
        return
    try:
        database.log_admin_action(
            admin_id=int(actor_id),
            action=action,
            target_type=target_type,
            target_id=target_id,
            old_value=_text(old),
            new_value=_text(new),
            reason=_text(reason),
            division_id=division_id,
        )
    except Exception as e:  # log_admin_action already swallows; belt and braces
        logger.warning("Admin journal write failed for %s: %s", action, e)


async def record(
    actor_id: int | None,
    action: str,
    target_type: str = "",
    target_id: int | None = None,
    *,
    old=None,
    new=None,
    division_id: int | None = None,
    reason: str | None = None,
) -> None:
    """Journal one admin action off the event loop. Never raises."""
    try:
        await asyncio.to_thread(
            record_sync, actor_id, action, target_type, target_id,
            old=old, new=new, division_id=division_id, reason=reason,
        )
    except Exception as e:
        logger.warning("Admin journal write failed for %s: %s", action, e)
