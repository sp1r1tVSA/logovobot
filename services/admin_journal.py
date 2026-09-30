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
import logging

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


def format_entry(row: dict) -> str:
    """One journal row as HTML: when, who, what, on what, old → new, reason."""
    esc = html.escape
    stamp = str(row.get("created_at") or "")
    # 'YYYY-MM-DD HH:MM:SS' (MSK) → 'DD.MM HH:MM'
    when = f"{stamp[8:10]}.{stamp[5:7]} {stamp[11:16]}" if len(stamp) >= 16 else stamp
    username = row.get("actor_username")
    who = f"@{esc(username)}" if username else f"<code>{row.get('actor_id')}</code>"
    target = ""
    if row.get("target_type"):
        target = f" · {esc(str(row['target_type']))}"
        if row.get("target_id"):
            target += f" #{row['target_id']}"
    if row.get("division_id"):
        target += f" · див. {row['division_id']}"
    lines = [f"<b>{esc(when)}</b> {who} — {esc(action_label(str(row.get('action') or '?')))}{target}"]
    old, new = row.get("old_value"), row.get("new_value")
    if old not in (None, "") and new not in (None, ""):
        lines.append(f"   {esc(_short(old))} → {esc(_short(new))}")
    elif new not in (None, ""):
        lines.append(f"   {esc(_short(new))}")
    elif old not in (None, ""):
        lines.append(f"   было: {esc(_short(old))}")
    if row.get("reason"):
        lines.append(f"   💬 {esc(_short(row['reason']))}")
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
