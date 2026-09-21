import os
import io
import json
import re
import urllib.request
import urllib.error
import asyncio
import datetime
import sqlite3
import uuid
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.error import BadRequest, TelegramError, Forbidden
from telegram.ext import ContextTypes, ConversationHandler
import html
import database
from time_utils import now_msk
from handlers.base import (
    is_admin,
    is_global_admin,
    admin_only,
    post_league_table_to_reports,
    resolve_division_target,
    round_schedule_missing_message,
    max_active_rounds_message,
)
from handlers.cabinet import notify_match_confirmed, safe_send_notification, cb_report_choice_manual, safe_edit_or_reply
import config
from config import MAX_WARNS_LIMIT, GROUP_ID

from handlers.squad_ai import offer_recognized_squad
from services.graphics import player_photos
from services import debt_lifecycle, debt_policy
from services.tournament_validator import RoundRobinValidator
from services.schedule_generator import (
    generate_asymmetric_round_robin_fixtures,
    generate_round_robin_fixtures,
)
import logging

logger = logging.getLogger(__name__)

WARN_REASONS = [
    "🔴 Долг (1 несыгранный матч / тур)",
    "Несвоевременный отчет",
    "Оскорбления / Неспортивное поведение",
    "Игнорирование соперника",
    "Нарушение регламента составов"
]
_warn_action_locks: set[int] = set()


# 🎰 Предсезонная линия БК всегда встаёт на первую пару туров: дальше её
# двигает автопилот «два через два» (database.advance_betting_line_pair).
PRESEASON_LINE_ROUNDS = (1, 2)


async def _open_preseason_line(div_id: int, season_id: int | None = None) -> list[int]:
    """Выставить линию Logovo.bet на Туры 1 и 2 дивизиона.

    Приём прогнозов открывается заранее — туры остаются `is_open = 0`, а
    `set_round_bets_open` сразу генерирует котировки на четыре центральных
    матча каждого тура. Возвращает список туров, на которые линия встала;
    ошибка по одному туру не мешает открыть второй.
    """
    opened: list[int] = []
    for r_num in PRESEASON_LINE_ROUNDS:
        try:
            ok = await asyncio.to_thread(
                database.set_round_bets_open, r_num, True, div_id, season_id
            )
            if ok:
                opened.append(r_num)
        except Exception as e:
            logger.warning(f"Could not open pre-season betting line for round {r_num} (div {div_id}): {e}")
    return opened


async def _send_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, markup: InlineKeyboardMarkup) -> None:
    """Отрисовать экран админки как ответ на сообщение или как правку callback-сообщения."""
    query = update.callback_query
    if query:
        try:
            await query.answer()
        except Exception:
            pass
        await safe_edit_or_reply(query, context, text, reply_markup=markup, parse_mode="HTML")
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)


async def _deny_access(update: Update, message: str = "⛔ Доступ запрещён") -> None:
    """Единый отказ в доступе для callback и текстовых входов."""
    query = update.callback_query
    if query:
        try:
            await query.answer(message, show_alert=True)
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(f"❌ {message}")


async def _ensure_division_access(update: Update, div_id: int) -> bool:
    """
    Проверка прав на конкретный дивизион — защита от подделки callback_data.
    Глобальные админы проходят всегда, админ дивизиона — только по своим дивизионам.
    """
    user = update.effective_user
    if not user:
        return False
    if is_global_admin(user.id):
        return True
    divisions = await asyncio.to_thread(database.get_admin_divisions, user.id)
    if div_id not in [d["id"] for d in divisions]:
        await _deny_access(update, "⛔ У вас нет прав на этот дивизион")
        return False
    return True


async def _ensure_super_admin(update: Update) -> bool:
    """Раздел только для супер-админа: is_admin() истинен и для админа дивизиона."""
    user = update.effective_user
    if not user or not is_global_admin(user.id):
        await _deny_access(update, "⛔ Раздел доступен только супер-админу")
        return False
    return True


async def _resolve_division_group_chat(div_id: int) -> int | None:
    """
    Группа, в которой живут топики дивизиона: сначала любой уже привязанный
    топик этого дивизиона, затем основная группа лиги. Нужна там, где админ
    вводит голый message_thread_id — без chat_id привязка нероутируема.
    """
    topics_map = await asyncio.to_thread(database.get_division_topics_map, div_id)
    for entry in topics_map.values():
        chat = entry.get("group_chat_id")
        if chat:
            return int(chat)
    main_group = await asyncio.to_thread(database.get_group_id)
    return int(main_group) if main_group else None


async def _ensure_match_access(update: Update, match: dict | None) -> bool:
    """
    Карточка матча и любые действия над ней — только для супер-админа или
    админа дивизиона этого матча. Без проверки админ дивизиона 1 мог открыть
    admin_view_match_<id> чужого матча и проставить по нему ТП/сброс.
    """
    user = update.effective_user
    if not user:
        return False
    if is_global_admin(user.id):
        return True
    div_id = (match or {}).get("division_id")
    if div_id is None:
        # Легаси-матч вне дивизионов остаётся за супер-админом.
        await _deny_access(update, "⛔ У вас нет прав на этот матч")
        return False
    return await _ensure_division_access(update, int(div_id))


def _build_super_admin_keyboard() -> InlineKeyboardMarkup:
    """
    Клавиатура супер-админки. Вынесена отдельно, потому что тумблер ИИ
    перерисовывает её на месте через edit_message_reply_markup.
    """
    chat_mode = database.get_config("chat_mode") or "temshik"
    mode_label = "Темшик 🍺" if chat_mode == "temshik" else "Булли 😈"
    ai_label = "🟢 ВКЛ" if database.is_ai_chat_enabled() else "🔴 ВЫКЛ"
    keyboard = [
        [InlineKeyboardButton("🏆 Дивизионы", callback_data="admin_divs_hub")],
        [InlineKeyboardButton("👔 Админы дивизионов", callback_data="admin_div_admins_hub")],
        [InlineKeyboardButton("👥 Управление игроками", callback_data="admin_manage_players")],
        [InlineKeyboardButton("🔗 Привязка клубов", callback_data="admin_bind_hub")],
        [InlineKeyboardButton("🔄 Обновить таблицы и стату", callback_data="admin_force_update")],
        [InlineKeyboardButton(f"🎭 Режим общения: {mode_label}", callback_data="admin_toggle_chat_mode")],
        [InlineKeyboardButton(f"🤖 ИИ Темшик: {ai_label}", callback_data="admin_toggle_ai_chat")],
        [InlineKeyboardButton("« Назад в меню", callback_data="main_menu")]
    ]
    return InlineKeyboardMarkup(keyboard)


@admin_only
async def show_super_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Полная админ-панель — только для глобальных (супер) админов."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _deny_access(update)
        return

    markup = await asyncio.to_thread(_build_super_admin_keyboard)
    text = "👑 <b>Админ-панель</b>\n\nВыберите раздел:"
    await _send_panel(update, context, text, markup)


@admin_only
async def show_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Точка входа в админку (RBAC-маршрутизатор).
    Супер-админ → полная панель; админ дивизиона → панель своего дивизиона
    (или выбор, если дивизионов несколько).
    """
    user = update.effective_user
    if not user:
        return

    if is_global_admin(user.id):
        await show_super_admin_panel(update, context)
        return

    divisions = await asyncio.to_thread(database.get_admin_divisions, user.id)
    if not divisions:
        await _deny_access(update, "⛔ У вас нет прав доступа к админ-панели")
        return

    if len(divisions) == 1:
        await show_division_admin_panel(update, context, divisions[0]["id"])
        return

    keyboard = [
        [InlineKeyboardButton(f"🛡 {d['name']}", callback_data=f"admin_div_panel:{d['id']}")]
        for d in divisions
    ]
    keyboard.append([InlineKeyboardButton("« Назад в меню", callback_data="main_menu")])
    text = "🛡 <b>Админ-панели дивизионов</b>\n\nВыберите дивизион для управления:"
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def show_division_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE, div_id: int | None = None) -> None:
    """Урезанная панель админа дивизиона: матчи, долги, варны."""
    user = update.effective_user
    query = update.callback_query
    if not user:
        return

    if div_id is None and query and query.data and ":" in query.data:
        try:
            div_id = int(query.data.split(":", 1)[1])
        except ValueError:
            div_id = None
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return

    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"

    keyboard = [
        [InlineKeyboardButton("⚔️ Управление матчами", callback_data=f"admin_div_manage_matches:{div_id}")],
        [InlineKeyboardButton("🔗 Привязка клубов", callback_data=f"admin_bind_div:{div_id}")],
        [InlineKeyboardButton("📢 Рассылка задолженностей", callback_data=f"admin_div_debts_menu:{div_id}")],
        [InlineKeyboardButton("👥 Выдача варнов", callback_data=f"admin_div_manage_players:{div_id}")],
    ]

    my_divisions = await asyncio.to_thread(database.get_admin_divisions, user.id)
    if len(my_divisions) > 1:
        keyboard.append([InlineKeyboardButton("🔁 Другой дивизион", callback_data="admin_main_menu")])
    keyboard.append([InlineKeyboardButton("« Назад в меню", callback_data="main_menu")])

    text = f"🛡 <b>Админ-панель дивизиона {html.escape(str(div_name))}</b>\n\nВыберите раздел:"
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_toggle_chat_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    current = database.get_config("chat_mode") or "temshik"
    new_mode = "persona2" if current == "temshik" else "temshik"
    database.set_config("chat_mode", new_mode)
    await query.message.reply_text(
        f"✅ Режим общения ИИ изменён: <b>{'Булли 😈' if new_mode == 'persona2' else 'Темшик 🍺'}</b>",
        parse_mode="HTML"
    )

async def admin_toggle_ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Мастер-выключатель генеративных ответов ИИ «Темшик» (экономия токенов Gemini,
    техработы, оффтоп). Клавиатура перерисовывается на месте, без нового сообщения.

    Без @admin_only намеренно: декоратор гасит callback пустым query.answer(),
    а Telegram принимает ответ на запрос только один раз — тост с новым состоянием
    тогда не долетает. Права проверяются вручную тем же _ensure_super_admin.
    """
    query = update.callback_query
    if not query:
        return

    if not await _ensure_super_admin(update):
        return

    new_state = not await asyncio.to_thread(database.is_ai_chat_enabled)
    await asyncio.to_thread(database.set_ai_chat_enabled, new_state)

    try:
        await query.answer("ИИ Темшик включён 🟢" if new_state else "ИИ Темшик выключен 🔴")
    except Exception as e:
        # Тост не критичен: состояние всё равно видно на перерисованной кнопке.
        logger.warning(f"Could not answer AI toggle callback: {e}")

    markup = await asyncio.to_thread(_build_super_admin_keyboard)
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except Exception as e:
        logger.warning(f"Could not refresh super admin keyboard after AI toggle: {e}")


@admin_only
async def admin_force_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force rechecks the DB, reconciles all division statistics, and sends audit summary to admin without touching group topics."""
    user = update.effective_user
    if not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    if update.callback_query:
        await update.callback_query.answer("🔄 Запущена сверка баз данных и статистики...")
    else:
        await update.message.reply_text("🔄 Запущена сверка баз данных и статистики...")

    try:
        # Complete audit & data reconciliation across all active divisions (no posts sent to groups)
        audit_res = await asyncio.to_thread(database.reconcile_all_divisions_data)

        # Build informative audit report for admin
        divs_cnt = audit_res.get("divisions_checked", 0)
        matches_cnt = audit_res.get("matches_checked", 0)
        goals_cnt = audit_res.get("total_goals_in_matches", 0)
        events_cnt = audit_res.get("events_checked", 0)
        discrepancies = audit_res.get("discrepancies", [])

        if not discrepancies:
            msg = (
                "✅ <b>Сверка данных и баз успешно завершена!</b>\n\n"
                f"📊 <b>Итоги аудита дивизионов:</b>\n"
                f"• Активных дивизионов: <b>{divs_cnt}</b>\n"
                f"• Подтверждённых матчей: <b>{matches_cnt}</b>\n"
                f"• Голов в матчах: <b>{goals_cnt}</b>\n"
                f"• Событий игроков (match_events): <b>{events_cnt}</b>\n"
                "• Расхождений и ошибок: <b>0</b> ✅"
            )
        else:
            disc_preview = "\n".join(f"• {html.escape(d)}" for d in discrepancies[:10])
            if len(discrepancies) > 10:
                disc_preview += f"\n<i>...и ещё {len(discrepancies) - 10} замечаний</i>"
            msg = (
                "⚠️ <b>Сверка завершена с замечаниями:</b>\n\n"
                f"📊 <b>Итоги аудита дивизионов:</b>\n"
                f"• Активных дивизионов: <b>{divs_cnt}</b>\n"
                f"• Подтверждённых матчей: <b>{matches_cnt}</b>\n"
                f"• Голов в матчах: <b>{goals_cnt}</b>\n"
                f"• Найдено расхождений: <b>{len(discrepancies)}</b>\n\n"
                f"❗️ <b>Список замечаний:</b>\n{disc_preview}"
            )

        if update.callback_query:
            await update.callback_query.message.reply_text(msg, parse_mode="HTML")
        elif update.message:
            await update.message.reply_text(msg, parse_mode="HTML")
    except Exception as e:
        logger.error(f"Error in force_update: {e}")
        err_msg = "❌ Ошибка при сверке баз данных. Проверьте логи."
        if update.callback_query:
            await update.callback_query.message.reply_text(err_msg)
        elif update.message:
            await update.message.reply_text(err_msg)

@admin_only
async def admin_list_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.edit_message_text("❌ У вас нет прав.")
        return

    players = await asyncio.to_thread(database.list_users)
    keyboard = [[InlineKeyboardButton("« Назад", callback_data="admin_main_menu")]]
    markup = InlineKeyboardMarkup(keyboard)

    if not players:
        await query.edit_message_text("👥 Нет зарегистрированных игроков.", reply_markup=markup)
        return

    lines = ["👥 <b>Зарегистрированные игроки:</b>\n"]
    for i, p in enumerate(players, start=1):
        username_str = f"@{html.escape(p['username'])}" if p['username'] else "(без юзернейма)"
        team_str = f" [{html.escape(p['team_name'])}]" if p['team_name'] else ""
        lines.append(f"{i}. {username_str}{team_str} <code>ID: {p['telegram_id']}</code>")

    await query.edit_message_text("\n".join(lines), parse_mode="HTML", reply_markup=markup)



@admin_only
async def admin_test_ai(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run interactive diagnostic check for WARP proxy and Gemini AI models."""
    msg = update.message or (update.callback_query.message if update.callback_query else None)
    if not msg:
        return

    status_msg = await msg.reply_text("🔄 <b>Запуск диагностики связи с WARP и Gemini AI...</b>", parse_mode="HTML")

    import urllib.request
    import urllib.error
    from services.ai.ai_recognizer import GEMINI_MODELS, _check_proxy_alive
    import config

    target_api_key = (getattr(config, "GEMINI_API_KEY", "") or "").strip()
    if not target_api_key:
        await status_msg.edit_text(
            "🤖 <b>РЕЗУЛЬТАТЫ ДИАГНОСТИКИ AI &amp; WARP</b>\n\n"
            "❌ <code>GEMINI_API_KEY не установлен в config.py!</code>",
            parse_mode="HTML",
        )
        return

    def _run_diagnostics() -> tuple[bool, list[str]]:
        """Blocking proxy probe + per-model reachability check. Runs off the event loop."""
        alive = _check_proxy_alive("http://127.0.0.1:4001")
        proxy_url = "http://127.0.0.1:4001" if alive else None
        results: list[str] = []

        for m_name in GEMINI_MODELS:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{m_name}:generateContent?key={target_api_key}"
            payload = {"contents": [{"parts": [{"text": "Reply OK"}]}]}
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}
            )

            if proxy_url:
                handler = urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
                opener = urllib.request.build_opener(handler)
            else:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

            try:
                with opener.open(req, timeout=8) as res:
                    res_data = json.loads(res.read().decode("utf-8"))
                    if res_data.get("candidates"):
                        results.append(f"• <code>{m_name}</code>: ✅ 200 OK")
                    else:
                        results.append(f"• <code>{m_name}</code>: ⚠️ Нет ответа")
            except urllib.error.HTTPError as e:
                err_text = e.read().decode("utf-8", errors="ignore")[:60].replace("\n", " ")
                results.append(f"• <code>{m_name}</code>: ❌ HTTP {e.code} ({html.escape(err_text)})")
            except Exception as e:
                results.append(f"• <code>{m_name}</code>: ❌ {html.escape(str(e))}")

        return alive, results

    warp_alive, model_lines = await asyncio.to_thread(_run_diagnostics)
    warp_status_str = "✅ <b>Доступен (127.0.0.1:4001)</b>" if warp_alive else "❌ <b>Не прослушивается (прямой режим)</b>"

    lines = [
        "🤖 <b>РЕЗУЛЬТАТЫ ДИАГНОСТИКИ AI &amp; WARP</b>\n",
        f"📡 <b>WARP Proxy Status:</b> {warp_status_str}\n",
        "🧪 <b>Статус моделей Gemini:</b>",
        *model_lines,
    ]

    await status_msg.edit_text("\n".join(lines), parse_mode="HTML")

# --- Broadcast Handlers (Debt Notifications) ---

async def _build_debts_summary(division_id: int | None = None, season_id: int | None = None, division_name: str | None = None) -> tuple[str | None, int]:
    """
    Build a full HTML summary of outstanding debts grouped by participant (club).
    Optionally scoped to a specific division and season.
    Returns (text, total_debts_count). text is None when there are no debts.
    """
    if division_id is not None:
        league_unplayed, users = await asyncio.gather(
            asyncio.to_thread(database.get_all_unplayed_league_matches, division_id=division_id, season_id=season_id),
            asyncio.to_thread(database.get_division_users, division_id),
        )
    else:
        league_unplayed, users = await asyncio.gather(
            asyncio.to_thread(database.get_all_unplayed_league_matches, division_id=division_id, season_id=season_id),
            asyncio.to_thread(database.list_users),
        )

    if not league_unplayed:
        return None, 0

    # Map club name (lowercased) -> user info to group debts by participant
    user_by_team: dict[str, dict] = {}
    for u in users:
        u_dict = dict(u) if isinstance(u, sqlite3.Row) else dict(u) if hasattr(u, "keys") else u
        team = (u_dict.get("team_name") or "").strip()
        if team:
            w_cnt = int(u_dict.get("warn_count") or 0)
            user_by_team.setdefault(
                team.lower(),
                {
                    "telegram_id": u_dict.get("telegram_id"),
                    "username": u_dict.get("username"),
                    "team_name": team,
                    "warn_count": w_cnt,
                }
            )

    participants: dict[str, dict] = {}

    def ensure_participant(team: str | None) -> dict | None:
        if not team:
            return None
        info = user_by_team.get(team.strip().lower())
        p = participants.setdefault(
            team.strip().lower(),
            {
                "team_name": team,
                "username": info["username"] if info else None,
                "warn_count": info["warn_count"] if info else 0,
                "league": [],
            },
        )
        return p

    for m in league_unplayed:
        p1 = ensure_participant(m.get("player1_team") or m.get("p1_team"))
        p2 = ensure_participant(m.get("player2_team") or m.get("p2_team"))
        t1 = html.escape(m.get('player1_team') or m.get('p1_team') or 'неизвестно')
        t2 = html.escape(m.get('player2_team') or m.get('p2_team') or 'неизвестно')
        u1 = f" (@{html.escape(m['p1_username'])})" if m.get('p1_username') else ""
        u2 = f" (@{html.escape(m['p2_username'])})" if m.get('p2_username') else ""
        line = f"Тур {m['round_number']}: 🏠 <b>{t1}</b>{u1} -:- <b>{t2}</b>{u2} ✈️"
        if p1:
            p1["league"].append(line)
        if p2:
            p2["league"].append(line)

    total_debts = len(league_unplayed)

    now_str = now_msk().strftime("%d.%m.%Y %H:%M")
    header_title = f"🗂 <b>ДОЛГИ УЧАСТНИКОВ — {html.escape(division_name.upper())}</b>\n" if division_name else "🗂 <b>ДОЛГИ УЧАСТНИКОВ</b>\n"
    lines = [
        header_title,
        f"<i>Обновлено: {now_str}</i>\n",
    ]

    bar = "━━━━━━━━━━━━━━━━━━━━━━"

    for idx, p in enumerate(sorted(participants.values(), key=lambda x: (len(x["league"]), x.get("warn_count", 0)), reverse=True), 1):
        uname_str = f"@{p['username']}" if p['username'] else p['team_name']
        total_n = len(p["league"])
        w_cnt = p.get("warn_count", 0)
        warn_badge = f" ⚠️ <b>{w_cnt}/{MAX_WARNS_LIMIT}</b>" if w_cnt > 0 else f" 🟢 <b>0/{MAX_WARNS_LIMIT}</b>"
        card: list[str] = [bar]
        card.append(f"{idx}. 👤 <b>{html.escape(uname_str)}</b> [{html.escape(p['team_name'])}] — {total_n} долг. |{warn_badge}")
        if p["league"]:
            card.append("⚙️ <b>МАТЧИ:</b>")
            for line in p["league"]:
                card.append(f"   • {line}")
        card.append(bar)
        lines.extend(card)
        lines.append("")

    lines.append("⏰ Пожалуйста, согласуйте время и сыграйте матчи! Несыгранные игры ведут к предупреждениям.")

    return "\n".join(lines), total_debts


MAX_DEBTS_MSG_LEN = 4000


def _chunk_debts_text(text: str) -> list[str]:
    """
    Split a (possibly too long) HTML debts summary into Telegram-safe chunks.
    Cuts only on participant-block boundaries (blank-line separated blocks), so every
    message shows complete, correctly ordered participant blocks. The summary header
    is repeated in each chunk and the closing reminder goes into the last one.
    """
    blocks = text.split("\n\n")
    if not blocks:
        return [""]

    header = blocks[0]
    footer = blocks[-1]
    body = blocks[1:-1]

    chunks: list[str] = []
    current = header + "\n\n"
    for block in body:
        piece = block + "\n\n"
        if len(piece) > MAX_DEBTS_MSG_LEN:
            for line in block.split("\n"):
                lp = line + "\n"
                if len(current) + len(lp) > MAX_DEBTS_MSG_LEN and len(current) > len(header):
                    chunks.append(current.rstrip())
                    current = header + "\n\n" + lp
                else:
                    current += lp
            current += "\n"
            continue
        if len(current) + len(piece) > MAX_DEBTS_MSG_LEN and len(current) > len(header):
            chunks.append(current.rstrip())
            current = header + "\n\n" + piece
        else:
            current += piece
    current += footer
    chunks.append(current.rstrip())
    return chunks or [""]


async def _delete_any_message(context, group_id: int, ids: list[int]) -> None:
    """Best-effort deletion of the given message ids in the target chat."""
    for mid in ids:
        try:
            await context.bot.delete_message(chat_id=group_id, message_id=mid)
        except (BadRequest, TelegramError):
            pass


async def _post_or_update_debts_for_division(context: ContextTypes.DEFAULT_TYPE, division_id: int, division_name: str) -> tuple[bool, int]:
    """
    Send or update debts summary for a specific division in its bound forum topic (previews or warns).
    Returns (success, debts_count).
    """
    text, total_debts = await _build_debts_summary(division_id=division_id, division_name=division_name)
    group_id, topic_id = await resolve_division_target(
        division_id, "previews", "warns", legacy_topic_keys=("warns_topic_id",)
    )
    if not group_id:
        return False, 0
    if not topic_id:
        return False, total_debts

    config_key = f"div_debts_msg_{division_id}"
    existing_raw = await asyncio.to_thread(database.get_config, config_key)
    existing_ids = [int(x) for x in str(existing_raw or "").split(",") if str(x).strip().isdigit()]

    if text is None:
        await _delete_any_message(context, group_id, existing_ids)
        if existing_ids:
            await asyncio.to_thread(database.set_config, config_key, "")
        return True, 0

    chunks = _chunk_debts_text(text)

    # Fast path: in-place edit
    if len(chunks) == len(existing_ids):
        try:
            new_ids: list[int] = []
            for i, chunk in enumerate(chunks):
                try:
                    await context.bot.edit_message_text(
                        chat_id=group_id, message_id=existing_ids[i], text=chunk, parse_mode="HTML"
                    )
                    new_ids.append(existing_ids[i])
                    continue
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        new_ids.append(existing_ids[i])
                        continue
                raise TelegramError("debts message cannot be edited in place")
            await asyncio.to_thread(database.set_config, config_key, ",".join(map(str, new_ids)))
            return True, total_debts
        except (BadRequest, TelegramError) as e:
            logger.warning(f"Division {division_id} debts summary needs rebuild ({e}); will re-post all messages")

    # Rebuild path
    await _delete_any_message(context, group_id, existing_ids)
    new_ids: list[int] = []
    try:
        for chunk in chunks:
            msg = await context.bot.send_message(
                chat_id=group_id, text=chunk, parse_mode="HTML", message_thread_id=int(topic_id)
            )
            new_ids.append(msg.message_id)
    except (BadRequest, TelegramError) as e:
        if new_ids:
            await asyncio.to_thread(database.set_config, config_key, ",".join(map(str, new_ids)))
        logger.warning(f"Failed to post debts to division {division_id} topic: {e}")
        return False, total_debts

    await asyncio.to_thread(database.set_config, config_key, ",".join(map(str, new_ids)))
    return True, total_debts


async def _post_or_update_debts_in_warns(context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Send debts summary to division topics or global warns thread, or edit the previously sent messages.
    """
    divisions = await asyncio.to_thread(database.get_active_divisions)
    if divisions:
        success_any = False
        for d in divisions:
            s, _ = await _post_or_update_debts_for_division(context, d["id"], d.get("name") or f"Дивизион {d['id']}")
            if s:
                success_any = True
        return success_any

    text, total_debts = await _build_debts_summary()
    group_id = GROUP_ID or await asyncio.to_thread(database.get_group_id)
    warns_topic_id = await asyncio.to_thread(database.get_config, "warns_topic_id")
    if not group_id or not warns_topic_id:
        return False

    existing_raw = await asyncio.to_thread(database.get_config, "warns_debts_msg_id")
    existing_ids = [int(x) for x in str(existing_raw or "").split(",") if str(x).strip().isdigit()]

    if text is None:
        await _delete_any_message(context, group_id, existing_ids)
        if existing_ids:
            await asyncio.to_thread(database.set_config, "warns_debts_msg_id", "")
        return True

    chunks = _chunk_debts_text(text)

    if len(chunks) == len(existing_ids):
        try:
            new_ids: list[int] = []
            for i, chunk in enumerate(chunks):
                try:
                    await context.bot.edit_message_text(
                        chat_id=group_id, message_id=existing_ids[i], text=chunk, parse_mode="HTML"
                    )
                    new_ids.append(existing_ids[i])
                    continue
                except BadRequest as e:
                    if "message is not modified" in str(e).lower():
                        new_ids.append(existing_ids[i])
                        continue
                raise TelegramError("debts message cannot be edited in place")
            await asyncio.to_thread(database.set_config, "warns_debts_msg_id", ",".join(map(str, new_ids)))
            return True
        except (BadRequest, TelegramError) as e:
            logger.warning(f"Debts summary needs rebuild ({e}); will re-post all messages")

    await _delete_any_message(context, group_id, existing_ids)
    new_ids: list[int] = []
    try:
        for chunk in chunks:
            msg = await context.bot.send_message(
                chat_id=group_id, text=chunk, parse_mode="HTML", message_thread_id=int(warns_topic_id)
            )
            new_ids.append(msg.message_id)
    except (BadRequest, TelegramError) as e:
        if new_ids:
            await asyncio.to_thread(database.set_config, "warns_debts_msg_id", ",".join(map(str, new_ids)))
        logger.warning(f"Failed to post debts to ПРЕДЫ thread: {e}")
        return False

    await asyncio.to_thread(database.set_config, "warns_debts_msg_id", ",".join(map(str, new_ids)))
    return True

# --- Match Generation Handlers ---

@admin_only
async def admin_gen_div_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display confirmation screen before wiping and generating fixtures for the selected division."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    try:
        div_id = int(query.data.replace("admin_gen_div_", ""))
    except ValueError:
        await _deny_access(update, "⛔ Дивизион не определён")
        return

    user_id = query.from_user.id
    if not (is_global_admin(user_id) or database.is_division_admin(user_id, div_id)):
        await query.answer("❌ У вас нет прав для управления расписанием этого дивизиона!", show_alert=True)
        return

    d = await asyncio.to_thread(database.get_division, div_id)
    div_title = d["name"] if d else f"Дивизион {div_id}"

    users = await asyncio.to_thread(database.get_division_users, div_id)
    with_team = [u for u in users if u.get("team_name")]

    if len(with_team) < 2:
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await query.edit_message_text(
            f"❌ <b>Недостаточно участников!</b>\n\n"
            f"В дивизионе <b>{html.escape(div_title)}</b> всего {len(with_team)} игрок(ов) с назначенным клубом.\n"
            f"Для создания расписания Round Robin требуется минимум 2 участника.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    keyboard = [
        [InlineKeyboardButton("✅ Подтвердить и сгенерировать", callback_data=f"admin_gen_exec_{div_id}")],
        [InlineKeyboardButton("« Отмена", callback_data=f"admin_div_manage_matches:{div_id}")]
    ]

    text = (
        f"⚠️ <b>Подтверждение генерации расписания</b>\n\n"
        f"• Дивизион: <b>{html.escape(div_title)}</b>\n"
        f"• Готовых участников: <b>{len(with_team)}</b>\n\n"
        f"⚠️ <i>Внимание: Существующие матчи <b>ТОЛЬКО</b> этого дивизиона будут сброшены и сгенерированы заново:\n"
        f"• <b>30 туров</b> по системе Round Robin (2 круга по 8 матчей).\n"
        f"• <b>Асимметричный 2-й круг</b> (Туры 16–30): справедливый календарь без прямого зеркала, с разрывом очных встреч ≥ 5 туров и контролем серий дом/выезд.\n"
        f"• <b>Жеребьевка</b>: случайное распределение участников по календарной сетке.\n"
        f"Матчи и результаты других дивизионов затронуты НЕ будут!</i>"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


@admin_only
async def admin_generate_matches_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute match generation for a division."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    user_id = query.from_user.id
    try:
        div_id = int(query.data.replace("admin_gen_exec_", ""))
    except ValueError:
        await _deny_access(update, "⛔ Дивизион не определён")
        return

    d = await asyncio.to_thread(database.get_division, div_id)
    div_title = d["name"] if d else f"Дивизион {div_id}"

    # RBAC: caller must be Global Admin or assigned Division Admin for this division
    if not (is_global_admin(user_id) or database.is_division_admin(user_id, div_id)):
        await query.answer("❌ У вас нет прав для управления расписанием этого дивизиона!", show_alert=True)
        return

    # Resolve active season
    active_season = await asyncio.to_thread(database.get_active_season)
    season_id = active_season["id"] if active_season else 1

    # Protection: check if division already has confirmed/completed matches in this season
    has_played = await asyncio.to_thread(database.division_has_played_matches, div_id, season_id)
    if has_played:
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await query.edit_message_text(
            f"🚫 <b>Генерация заблокирована!</b>\n\n"
            f"В дивизионе <b>{html.escape(div_title)}</b> уже есть сыгранные или подтверждённые матчи в текущем сезоне.\n"
            f"Повторная генерация расписания запрещена для защиты целостности турнирных данных.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    users = await asyncio.to_thread(database.get_division_users, div_id)
    players = [p['telegram_id'] for p in users if p.get('team_name')]

    if len(players) < 2:
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await query.edit_message_text(
            f"❌ <b>Ошибка генерации:</b>\n\nНеобходимо как минимум 2 зарегистрированных игрока с заполненными профилями.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML"
        )
        return

    # Generate asymmetric round robin with randomized draw
    fixtures = generate_round_robin_fixtures(players, shuffle_teams=True)

    # Validate fixtures if standard 16 teams format
    if len(players) == 16:
        is_valid, validation_errors = RoundRobinValidator.validate_fixtures(
            fixtures,
            expected_teams=16,
            expected_rounds=30,
            expected_matches=240,
            division_id=div_id,
            season_id=season_id,
            check_asymmetric=True
        )
        if not is_valid:
            keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
            err_msg = "\n• ".join(validation_errors[:5])
            await query.edit_message_text(
                f"❌ <b>Ошибка валидации расписания:</b>\n\n• {html.escape(err_msg)}",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )
            return

    # Safe clear and insert for this division & season
    await asyncio.to_thread(database.clear_matches_by_division, div_id, season_id)
    await asyncio.to_thread(database.batch_insert_matches, fixtures, division_id=div_id, season_id=season_id)

    total_rounds = max(f[0] for f in fixtures) if fixtures else 0

    await asyncio.to_thread(
        database.log_admin_action,
        admin_id=user_id,
        action="generate_round_robin",
        target_type="division",
        target_id=div_id,
        division_id=div_id,
        season_id=season_id,
        metadata=f"Generated {len(fixtures)} matches across {total_rounds} rounds for {div_title} (asymmetric 2nd leg)"
    )

    # 🎰 Автопилот линии «два через два»: расписание есть — значит предсезонная
    # линия сразу встаёт на Туры 1 и 2 (is_open = 0, bets_open = 1). Ошибка
    # здесь не должна отменять уже сгенерированное расписание.
    line_rounds = await _open_preseason_line(div_id, season_id)
    if line_rounds:
        try:
            from services.betting_notifications import notify_division_betting_line_opened
            for r_num in line_rounds:
                await notify_division_betting_line_opened(context, div_id, r_num)
        except Exception as e:
            logger.warning(f"Failed to send betting line notification after round robin: {e}")

    keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
    if line_rounds:
        line_note = f"🎰 Линия Logovo.bet открыта на Туры: <b>{', '.join(str(r) for r in line_rounds)}</b>."
    else:
        line_note = "⚠️ Линию Logovo.bet открыть не удалось — сделайте это вручную в карточке тура."
    await query.edit_message_text(
        f"📅 <b>Расписание успешно сгенерировано!</b>\n\n"
        f"• Дивизион: <b>{html.escape(div_title)}</b>\n"
        f"• Участников: <b>{len(players)}</b>\n"
        f"• Всего туров: <b>{total_rounds}</b>\n"
        f"• Всего матчей: <b>{len(fixtures)}</b>\n"
        f"• Формат: <b>Асимметричный календарь</b> (30 туров, разрыв очных встреч ≥ 5 туров, баланс дом/выезд)\n\n"
        f"Матчи и туры дивизиона занесены в базу данных.\n"
        f"{line_note}",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML"
    )

    # Notify division topic or main group
    group_id, topic_id = await resolve_division_target(div_id, "drafts")
    if group_id:
        group_text = (
            f"📅 <b>Старт сезона в дивизионе {html.escape(div_title)}!</b>\n\n"
            f"Администратор сгенерировал расписание матчей.\n"
            f"• Участников: {len(players)}\n"
            f"• Всего туров: {total_rounds}\n\n"
            f"Свои матчи вы можете посмотреть в личном кабинете бота в разделе «📋 Мои матчи»."
        )
        try:
            await context.bot.send_message(
                chat_id=group_id,
                message_thread_id=topic_id,
                text=group_text,
                parse_mode="HTML"
            )
        except Exception as e:
            logger.exception("Не удалось отправить уведомление о генерации в группу")

# Conversation States for Admin Player management
ADMIN_EXPECT_PLAYER_USERNAME = 201
ADMIN_EXPECT_PLAYER_CLUB = 202
ADMIN_EXPECT_IMPORT_TEXT = 203
ADMIN_EXPECT_NEW_CLUB = 204
ADMIN_EXPECT_NEW_USERNAME = 206
ADMIN_EXPECT_NEW_NICKNAME = 207
ADMIN_EXPECT_RESET_CONFIRM = 208
ADMIN_EXPECT_PLAYER_DIVISION = 213
ADMIN_EXPECT_MANUAL_CLUB = 214

# Conversation States for Admin Match management
ADMIN_EXPECT_MATCH_SCORE = 205
ADMIN_WAITING_FOR_DEADLINE = 209
# 210 — бывший ADMIN_WAITING_FOR_BATCH_ROUNDS (ручной ввод диапазона туров).
# Пару туров теперь подбирает `get_next_rounds_to_open`, шаг убран; номер не
# переиспользуем, чтобы висящие диалоги старой версии не попали в чужое состояние.
ADMIN_WAITING_FOR_BATCH_DEADLINE = 211

# Conversation States for Admin Division management
ADMIN_EXPECT_DIV_NAME = 230
ADMIN_EXPECT_DIV_RENAME = 231
ADMIN_EXPECT_DIV_TOPIC_ID = 232

@admin_only
async def admin_manage_players_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show participant management hub menu."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    text = (
        "👥 **Управление участниками лиги**\n\n"
        "Выберите желаемое действие для управления списком игроков:"
    )
    keyboard = [
        [InlineKeyboardButton("📋 Список участников", callback_data="admin_list_players_page_0")],
        [InlineKeyboardButton("🏆 Дивизионы и участники", callback_data="admin_div_players_menu")],
        [InlineKeyboardButton("➕ Добавить игрока", callback_data="admin_add_player_start")],
        [InlineKeyboardButton("📊 Импорт списка участников", callback_data="admin_import_players_start")],
        [InlineKeyboardButton("⚠️ Сбросить лигу (Очистить всех)", callback_data="admin_clear_league_start")],
        [InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

@admin_only
async def admin_div_players_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hub menu showing participant counts by division and allowing filtered listing."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    if context.user_data is not None:
        context.user_data["admin_list_div_back"] = "admin_div_players_menu"

    divisions = await asyncio.to_thread(database.get_divisions)
    all_users = await asyncio.to_thread(database.list_users)

    div_counts = {}
    unassigned_count = 0
    for u in all_users:
        did = u.get("division_id") if isinstance(u, dict) else u["division_id"]
        if did is None:
            unassigned_count += 1
        else:
            div_counts[did] = div_counts.get(did, 0) + 1

    lines = ["🏆 <b>Распределение участников по дивизионам:</b>\n"]
    keyboard = []

    for d in divisions:
        did = d["id"]
        cnt = div_counts.get(did, 0)
        status_icon = "🟢" if d.get("is_active") else "⚪"
        lines.append(f"{status_icon} <b>{html.escape(d['name'])}:</b> {cnt} участников")
        keyboard.append([InlineKeyboardButton(f"👥 {d['name']} ({cnt})", callback_data=f"admin_list_div_players_{did}_0")])

    lines.append(f"⚪ <b>Без дивизиона:</b> {unassigned_count} участников")
    keyboard.append([InlineKeyboardButton(f"👥 Без дивизиона ({unassigned_count})", callback_data="admin_list_div_players_none_0")])
    keyboard.append([InlineKeyboardButton("« Назад к участникам", callback_data="admin_manage_players_info")])

    text = "\n".join(lines)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def _load_div_players_page(div_raw: str, page: int) -> tuple[list[dict], str, int, int, int]:
    """
    Подготовить данные для постраничного списка участников дивизиона.
    Возвращает (игроки_страницы, название_дивизиона, всего_игроков, страница, всего_страниц).
    """
    target_div_id = None if div_raw == "none" else int(div_raw)
    players = await asyncio.to_thread(database.get_division_users, target_div_id)

    div_title = "Без дивизиона"
    if target_div_id is not None:
        div_row = await asyncio.to_thread(database.get_division, target_div_id)
        if div_row:
            div_title = div_row["name"]

    if not players:
        return [], div_title, 0, 0, 0

    per_page = 8
    total_pages = (len(players) + per_page - 1) // per_page
    page = max(0, min(page, total_pages - 1))
    start_idx = page * per_page
    return players[start_idx:start_idx + per_page], div_title, len(players), page, total_pages


def _div_player_buttons(page_players: list[dict]) -> list[list[InlineKeyboardButton]]:
    """Кнопки-карточки участников для списка дивизиона."""
    keyboard = []
    for p in page_players:
        username_val = p['username'] or str(p['telegram_id'])
        team_val = f" ({p['team_name']})" if p['team_name'] else ""
        btn_text = f"👤 @{username_val}{team_val}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_player_{p['telegram_id']}")])
    return keyboard


@admin_only
async def admin_list_div_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show paginated list of players filtered by division."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    # Callback format: admin_list_div_players_{div_id}_{page}
    parts = query.data.replace("admin_list_div_players_", "").rsplit("_", 1)
    div_raw = parts[0]
    page = int(parts[1]) if len(parts) > 1 else 0

    if context.user_data is not None:
        context.user_data["admin_player_back_cb"] = f"admin_list_div_players_{div_raw}_{page}"

    back_cb = "admin_div_players_menu"
    back_text = "« К дивизионам"
    if div_raw != "none" and div_raw.isdigit():
        custom_back = context.user_data.get("admin_list_div_back") if context.user_data else None
        if custom_back != "admin_div_players_menu":
            back_cb = _div_home_cb(update, int(div_raw))
            back_text = "« К дивизиону" if update.effective_user and is_global_admin(update.effective_user.id) else "« Назад в панель"

    page_players, div_title, total, page, total_pages = await _load_div_players_page(div_raw, page)

    if not page_players:
        keyboard = [[InlineKeyboardButton(back_text, callback_data=back_cb)]]
        await query.edit_message_text(f"👥 В «{html.escape(div_title)}» нет участников.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = _div_player_buttons(page_players)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_list_div_players_{div_raw}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_list_div_players_{div_raw}_{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(back_text, callback_data=back_cb)])

    text = f"📋 <b>Участники: {html.escape(div_title)}</b> (Всего: {total}):\n\nВыберите игрока:"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


# --- RBAC: назначение админов дивизионов (только супер-админ) ---

ADMIN_EXPECT_DIV_ADMIN_REF = 233


def _parse_div_arg(query, prefix: str) -> int | None:
    """Достать division_id из callback_data вида `{prefix}:{div_id}` (или `{prefix}:{div_id}:{...}`)."""
    if not query or not query.data:
        return None
    raw = query.data[len(prefix):] if query.data.startswith(prefix) else query.data
    raw = raw.lstrip(":")
    part = raw.split(":", 1)[0]
    try:
        return int(part)
    except (TypeError, ValueError):
        return None


def _parse_div_round_arg(query) -> tuple[int | None, int | None]:
    """Достать (division_id, round_number) из callback_data вида `{prefix}:{div}:{round}`."""
    if not query or not query.data:
        return None, None
    parts = query.data.split(":")
    if len(parts) < 3:
        return None, None
    try:
        return int(parts[1]), int(parts[2])
    except (TypeError, ValueError):
        return None, None


def _div_home_cb(update: Update, div_id: int) -> str:
    """«Домашний» экран дивизиона для того, кто нажал кнопку.

    Карточка `admin_div_view` — экран супер-админа; админа дивизиона она отошьёт,
    поэтому его возвращаем в его собственную панель.
    """
    user = update.effective_user
    if user and is_global_admin(user.id):
        return f"admin_div_view_{div_id}"
    return f"admin_div_panel:{div_id}"


def _round_back_cb(context: ContextTypes.DEFAULT_TYPE, round_number: int) -> str:
    """Возврат с экранов тура — в карточку тура своего дивизиона.

    Дивизион в сессии не сохранён (бот перезапущен, устаревшее сообщение) —
    уводим в админку: глобального экрана матчей больше нет, а `admin_main_menu`
    в отличие от хаба дивизионов не отошьёт админа дивизиона.
    """
    div_id = context.user_data.get("admin_round_div_id") if context.user_data else None
    return f"admin_div_round:{div_id}:{round_number}" if div_id else "admin_main_menu"


async def _division_reports_topic(div_id: int) -> tuple[int | None, int | None]:
    """(group_chat_id, message_thread_id) топика «📞 ОТЧЁТЫ» дивизиона.

    Цепочка разрешения повторяет post_league_table_to_reports: сначала кэш
    топиков, затем БД; «tables» — исторический алиас того же топика.
    """
    from services.topic_cache import topic_cache

    div_topic = topic_cache.get_by_division(div_id, "reports") or topic_cache.get_by_division(div_id, "tables")
    if not div_topic:
        topics_map = await asyncio.to_thread(database.get_division_topics_map, div_id)
        div_topic = topics_map.get("reports") or topics_map.get("tables")

    if not div_topic or not div_topic.get("group_chat_id") or not div_topic.get("message_thread_id"):
        return None, None
    return div_topic["group_chat_id"], div_topic["message_thread_id"]


async def _announce_rounds_opened(
    context: ContextTypes.DEFAULT_TYPE, div_id: int, text: str, include_table: bool
) -> bool:
    """Объявить открытие тура(ов) в топике «📞 ОТЧЁТЫ» дивизиона.

    Возвращает False, если топик не привязан или отправка не удалась — открытие
    тура при этом не откатывается, админ просто получает предупреждение.
    """
    group_id, thread_id = await _division_reports_topic(div_id)
    if not group_id or not thread_id:
        logger.warning(f"No reports/tables topic configured for division {div_id}; skipping round announcement.")
        return False
    try:
        await context.bot.send_message(
            chat_id=group_id,
            message_thread_id=int(thread_id),
            text=text,
            parse_mode="HTML",
        )
        if include_table:
            await post_league_table_to_reports(context, division_id=div_id)
        return True
    except Exception:
        logger.exception(f"Failed to announce opened rounds for division {div_id}")
        return False


@admin_only
async def admin_div_admins_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Список дивизионов для управления их администраторами."""
    user = update.effective_user
    if not user or not is_global_admin(user.id):
        await _deny_access(update, "⛔ Раздел доступен только супер-админу")
        return

    divisions = await asyncio.to_thread(database.get_divisions)
    keyboard = []
    for d in divisions:
        admins = await asyncio.to_thread(database.get_division_admins, d["id"])
        status_icon = "🟢" if d.get("is_active") else "⚪"
        btn_text = f"{status_icon} {d['name']} — админов: {len(admins)}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_div_admins_view_{d['id']}")])
    keyboard.append([InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")])

    text = (
        "👔 <b>Админы дивизионов</b>\n\n"
        "Админ дивизиона видит только свой дивизион и управляет матчами, "
        "долгами и варнами внутри него.\n\n"
        "Выберите дивизион:"
    )
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_admins_view(update: Update, context: ContextTypes.DEFAULT_TYPE, div_id: int | None = None) -> None:
    """Текущие админы дивизиона + назначение/снятие прав."""
    user = update.effective_user
    if not user or not is_global_admin(user.id):
        await _deny_access(update, "⛔ Раздел доступен только супер-админу")
        return

    query = update.callback_query
    if div_id is None and query and query.data:
        try:
            div_id = int(query.data.replace("admin_div_admins_view_", ""))
        except ValueError:
            div_id = None
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    if not div:
        keyboard = [[InlineKeyboardButton("« К списку дивизионов", callback_data="admin_div_admins_hub")]]
        await _send_panel(update, context, "❌ Дивизион не найден.", InlineKeyboardMarkup(keyboard))
        return

    admins = await asyncio.to_thread(database.get_division_admins_detailed, div_id)

    lines = [f"👔 <b>Админы дивизиона {html.escape(div['name'])}</b>\n"]
    keyboard = []
    if admins:
        for a in admins:
            label = f"@{a['username']}" if a.get("username") else str(a["user_id"])
            team_str = f" [{a['team_name']}]" if a.get("team_name") else ""
            lines.append(f"• {html.escape(label)}{html.escape(team_str)} — <code>{a['user_id']}</code>")
            btn_text = f"➖ Снять {label}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_div_admin_del_{div_id}_{a['user_id']}")])
    else:
        lines.append("<i>Пока не назначено ни одного админа.</i>")

    keyboard.append([InlineKeyboardButton("➕ Назначить админа", callback_data=f"admin_div_admin_add_{div_id}")])
    keyboard.append([InlineKeyboardButton("« К списку дивизионов", callback_data="admin_div_admins_hub")])

    await _send_panel(update, context, "\n".join(lines), InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять права админа дивизиона."""
    user = update.effective_user
    query = update.callback_query
    if not user or not is_global_admin(user.id):
        await _deny_access(update, "⛔ Раздел доступен только супер-админу")
        return
    if not query or not query.data:
        return

    try:
        div_raw, uid_raw = query.data.replace("admin_div_admin_del_", "").split("_", 1)
        div_id = int(div_raw)
        target_id = int(uid_raw)
    except ValueError:
        await _deny_access(update, "⛔ Некорректные данные")
        return

    await asyncio.to_thread(database.remove_division_admin, div_id, target_id)
    logger.info(f"Division admin revoked: user={target_id} division={div_id} by={user.id}")
    await admin_div_admins_view(update, context, div_id=div_id)


@admin_only
async def admin_div_admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """FSM: запросить @username будущего админа дивизиона."""
    user = update.effective_user
    query = update.callback_query
    if not user or not is_global_admin(user.id):
        await _deny_access(update, "⛔ Раздел доступен только супер-админу")
        return ConversationHandler.END
    if not query or not query.data:
        return ConversationHandler.END

    try:
        div_id = int(query.data.replace("admin_div_admin_add_", ""))
    except ValueError:
        await _deny_access(update, "⛔ Дивизион не определён")
        return ConversationHandler.END

    context.user_data["div_admin_target_div"] = div_id
    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"

    keyboard = [[InlineKeyboardButton("Отмена", callback_data=f"admin_div_admins_view_{div_id}")]]
    text = (
        f"➕ <b>Назначение админа дивизиона {html.escape(str(div_name))}</b>\n\n"
        "Отправьте <b>@username</b> участника (можно и его Telegram ID).\n"
        "Пользователь должен быть в базе бота."
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_EXPECT_DIV_ADMIN_REF


async def admin_div_admin_add_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """FSM: найти пользователя по @username и записать его в division_admins."""
    user = update.effective_user
    if not user or not is_global_admin(user.id):
        return ConversationHandler.END
    if not update.message or not update.message.text:
        return ADMIN_EXPECT_DIV_ADMIN_REF

    div_id = context.user_data.get("div_admin_target_div")
    if not div_id:
        await update.message.reply_text("⚠️ Дивизион не выбран. Откройте раздел «👔 Админы дивизионов» заново.")
        return ConversationHandler.END

    ref = update.message.text.strip()
    if ref.lower() in ("отмена", "cancel", "/cancel"):
        return await admin_div_admin_cancel(update, context)

    target = await asyncio.to_thread(database.find_user_by_ref, ref)
    if not target:
        await update.message.reply_text(
            "❌ Пользователь не найден в базе.\nПроверьте @username или пришлите Telegram ID."
        )
        return ADMIN_EXPECT_DIV_ADMIN_REF

    target_id = target["telegram_id"]
    await asyncio.to_thread(database.add_division_admin, div_id, target_id)
    logger.info(f"Division admin granted: user={target_id} division={div_id} by={user.id}")

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"
    label = f"@{target['username']}" if target.get("username") else str(target_id)

    context.user_data.pop("div_admin_target_div", None)
    keyboard = [[InlineKeyboardButton("« К админам дивизиона", callback_data=f"admin_div_admins_view_{div_id}")]]
    await update.message.reply_text(
        f"✅ {html.escape(label)} назначен админом дивизиона <b>{html.escape(str(div_name))}</b>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    if target_id > 0:
        await safe_send_notification(
            context.bot,
            target_id,
            f"🛡 Вам выданы права <b>админа дивизиона {html.escape(str(div_name))}</b>.\n"
            "Панель управления доступна в главном меню.",
            None
        )
    return ConversationHandler.END


@admin_only
async def admin_div_admin_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """FSM: отмена назначения админа дивизиона."""
    context.user_data.pop("div_admin_target_div", None)
    if update.message:
        await update.message.reply_text("❌ Назначение отменено.")
    await admin_div_admins_hub(update, context)
    return ConversationHandler.END


# --- RBAC: прямые (изолированные) точки входа для админа дивизиона ---

@admin_only
async def admin_div_manage_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Матчи конкретного дивизиона — без шага «Выберите дивизион»."""
    query = update.callback_query
    div_id = _parse_div_arg(query, "admin_div_manage_matches")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"
    rounds = await asyncio.to_thread(database.get_division_rounds, div_id)

    # Действия над расписанием дивизиона идут над сеткой туров: генерация,
    # массовое открытие и долги — всё в скоупе этого дивизиона.
    keyboard = [
        [InlineKeyboardButton("🎲 Сгенерировать матчи", callback_data=f"admin_gen_div_{div_id}")],
        [InlineKeyboardButton("📦 Открыть туры", callback_data=f"admin_batch_open_div:{div_id}")],
        [InlineKeyboardButton("⏰ Просроченные", callback_data=f"admin_div_overdue:{div_id}")],
    ]
    row = []
    for r in rounds:
        info = await asyncio.to_thread(database.get_round_info, r, div_id)
        status_icon = ROUND_PHASE_ICONS[debt_policy.round_phase(info, now_msk())]
        row.append(InlineKeyboardButton(f"{status_icon} Тур {r}", callback_data=f"admin_div_round:{div_id}:{r}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    home_cb = _div_home_cb(update, div_id)
    keyboard.append([InlineKeyboardButton("« Назад", callback_data=home_cb)])

    if rounds:
        text = f"⚔️ <b>Матчи дивизиона {html.escape(str(div_name))}</b>\n\nВыберите тур:"
    else:
        text = f"⚔️ <b>Матчи дивизиона {html.escape(str(div_name))}</b>\n\nМатчи ещё не созданы."

    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка тура дивизиона: статус, дедлайн, линия ставок, действия."""
    query = update.callback_query
    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return
    await _render_div_round_card(query, context, div_id, round_number)


ROUND_PHASE_ICONS = {
    debt_policy.ROUND_SCHEDULED: "⚪",
    debt_policy.ROUND_OPEN: "🟢",
    debt_policy.ROUND_OVERDUE: "🟠",
    debt_policy.ROUND_CLOSED: "🔴",
}
ROUND_PHASE_LABELS = {
    debt_policy.ROUND_SCHEDULED: "Не открыт",
    debt_policy.ROUND_OPEN: "Открыт",
    debt_policy.ROUND_OVERDUE: "Дедлайн прошёл — ждёт закрытия",
    debt_policy.ROUND_CLOSED: "Закрыт",
}


def _fmt_msk(value) -> str:
    """ДД.ММ.ГГГГ ЧЧ:ММ для datetime или хранимой строки времени."""
    dt = value if isinstance(value, datetime.datetime) else database.parse_flexible_datetime(value)
    return dt.strftime("%d.%m.%Y %H:%M") if dt else str(value or "—")


async def _render_div_round_card(query, context: ContextTypes.DEFAULT_TYPE, div_id: int, round_number: int) -> None:
    """Нарисовать карточку тура дивизиона. Callback query уже отвечен вызывающим."""
    # Экраны напоминаний и списка матчей тура ключуются одним номером тура;
    # дивизион для них берётся отсюда — см. _round_back_cb.
    if context.user_data is not None:
        context.user_data["admin_round_div_id"] = div_id

    info = await asyncio.to_thread(database.get_round_info, round_number, div_id)
    if not info:
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await query.edit_message_text("❌ Тур не найден в этом дивизионе.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    is_open = info["is_open"]
    deadline = info["deadline"]
    bets_open = bool(info.get("bets_open"))
    phase = debt_policy.round_phase(info, now_msk())

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"

    text = f"📅 <b>Управление: {round_number}-й Тур</b>\n"
    text += f"Дивизион: <b>{html.escape(str(div_name))}</b>\n\n"
    text += f"Статус: {ROUND_PHASE_ICONS[phase]} {ROUND_PHASE_LABELS[phase]}"
    if phase == debt_policy.ROUND_CLOSED and info.get("closed_at"):
        text += f" ({html.escape(_fmt_msk(info['closed_at']))})"
    text += "\n"
    if deadline and phase != debt_policy.ROUND_SCHEDULED:
        text += f"Дедлайн: {html.escape(str(deadline))}\n"
    elif is_open:
        text += "⚠️ Дедлайн не задан — долги по туру не начислятся. Задайте его кнопкой ниже.\n"
    if bets_open and not is_open:
        text += "Линия Logovo.bet: 🎰 открыта заранее (тур ещё не открыт для игры)\n"
    else:
        text += f"Линия Logovo.bet: {'🎰 открыта' if bets_open else '🚫 закрыта'}\n"

    keyboard = []
    if is_open:
        keyboard.append([InlineKeyboardButton("🔴 Закрыть тур", callback_data=f"admin_div_round_close:{div_id}:{round_number}")])
        keyboard.append([InlineKeyboardButton("🕒 Изменить дедлайн", callback_data=f"admin_div_round_open:{div_id}:{round_number}")])
        keyboard.append([InlineKeyboardButton("⏰ Напомнить должникам", callback_data=f"admin_remind_round_{round_number}")])
    elif phase == debt_policy.ROUND_CLOSED:
        # Закрытый тур уже породил долги — вернуть его в игру вправе только
        # глобальный админ; долги без вердикта при этом снимаются.
        user = getattr(query, "from_user", None)
        if user and is_global_admin(user.id):
            keyboard.append([InlineKeyboardButton("♻️ Переоткрыть тур", callback_data=f"admin_div_round_open:{div_id}:{round_number}")])
    else:
        keyboard.append([InlineKeyboardButton("🟢 Открыть тур (установить дедлайн)", callback_data=f"admin_div_round_open:{div_id}:{round_number}")])
        # Ранняя линия: прогнозы можно принимать до открытия тура для игры.
        if bets_open:
            keyboard.append([InlineKeyboardButton("🚫 Закрыть линию ставок", callback_data=f"admin_div_bets_close:{div_id}:{round_number}")])
        else:
            keyboard.append([InlineKeyboardButton("🎰 Открыть линию ставок заранее", callback_data=f"admin_div_bets_open:{div_id}:{round_number}")])

    # Ручной перезапуск предсезонной линии: обычно она встаёт автоматически
    # сразу после генерации расписания, но кнопка нужна, если её закрывали.
    # Действие всегда про Туры 1-2, поэтому и кнопка — только на их карточках.
    if round_number in (1, 2) and phase == debt_policy.ROUND_SCHEDULED:
        keyboard.append([InlineKeyboardButton("🎰 Открыть линию на Туры 1-2", callback_data=f"admin_div_preseason_line:{div_id}")])
    keyboard.append([InlineKeyboardButton("⚔️ Смотреть матчи тура", callback_data=f"admin_div_round_matches:{div_id}:{round_number}")])
    keyboard.append([InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")])

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


@admin_only
async def admin_div_round_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Матчи одного тура внутри дивизиона."""
    query = update.callback_query
    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return
    if context.user_data is not None:
        context.user_data["admin_round_div_id"] = div_id

    matches = await asyncio.to_thread(database.get_matches_by_round, round_number, div_id)

    keyboard = []
    for m in matches:
        opp1 = m.get("player1_nickname") or m.get("player1_team") or "К1"
        opp2 = m.get("player2_nickname") or m.get("player2_team") or "К2"
        if m["status"] == "confirmed":
            status_lbl = f"{m['player1_score']}:{m['player2_score']}"
        elif m["status"] == "disputed":
            status_lbl = "⚠️ спор"
        else:
            status_lbl = "⚔️"
        btn_text = f"{opp1} vs {opp2} ({status_lbl})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_match_{m['id']}")])

    keyboard.append([InlineKeyboardButton("« К туру", callback_data=f"admin_div_round:{div_id}:{round_number}")])

    text = f"📅 <b>Матчи {round_number}-го тура</b>\n\n"
    text += "Выберите матч для ввода счёта или сброса:" if matches else "В этом туре матчей нет."
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_broadcast_debts(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Рассылка сводки долгов в топик своего дивизиона."""
    query = update.callback_query
    div_id = _parse_div_arg(query, "admin_div_broadcast_debts")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"Дивизион {div_id}"

    success, debts_cnt = await _post_or_update_debts_for_division(context, div_id, div_name)

    if success:
        text = (
            f"📢 <b>Сводка долгов отправлена</b>\n\n"
            f"Дивизион: {html.escape(str(div_name))}\n"
            f"Найдено долгов: <b>{debts_cnt}</b>"
        )
    else:
        text = (
            f"⚠️ <b>Не удалось отправить сводку</b>\n\n"
            f"Дивизион: {html.escape(str(div_name))}\n"
            "Топик дивизиона не настроен — обратитесь к супер-админу."
        )

    keyboard = [
        [InlineKeyboardButton("🔄 Отправить ещё раз", callback_data=f"admin_div_broadcast_debts:{div_id}")],
        [InlineKeyboardButton("« Назад", callback_data=f"admin_div_debts_menu:{div_id}")],
    ]
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_debts_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Развилка рассылки долгов дивизиона: ЛС должникам или сводка в топик.

    Два действия раньше назывались одинаково («Рассылка задолженностей») в разных
    панелях и делали разное — здесь они разведены явными подписями.
    """
    query = update.callback_query
    div_id = _parse_div_arg(query, "admin_div_debts_menu")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"Дивизион {div_id}"

    text = (
        f"📢 <b>Рассылка задолженностей — {html.escape(str(div_name))}</b>\n\n"
        "✉️ <b>ЛС должникам</b> — каждому участнику дивизиона уйдёт личное сообщение "
        "со списком именно его просроченных матчей.\n"
        "📋 <b>Сводка в топик</b> — общий список долгов дивизиона в его топик группы."
    )

    home_cb = _div_home_cb(update, div_id)
    keyboard = [
        [InlineKeyboardButton("✉️ ЛС должникам дивизиона", callback_data=f"admin_div_debts_dm:{div_id}")],
        [InlineKeyboardButton("📋 Сводка в топик дивизиона", callback_data=f"admin_div_broadcast_debts:{div_id}")],
        [InlineKeyboardButton("« Назад", callback_data=home_cb)],
    ]
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_debts_dm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Персональные ЛС должникам одного дивизиона."""
    query = update.callback_query
    div_id = _parse_div_arg(query, "admin_div_debts_dm")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"Дивизион {div_id}"

    overdue, members = await asyncio.gather(
        asyncio.to_thread(database.get_detailed_overdue_matches, div_id),
        asyncio.to_thread(database.get_division_users, div_id),
    )

    # get_detailed_overdue_matches подтягивает и legacy-игроков без дивизиона,
    # поэтому адресатов ограничиваем составом самого дивизиона.
    member_ids = {u["telegram_id"] for u in members if u.get("telegram_id")}

    debts_by_user: dict[int, list[str]] = {}
    for m in overdue:
        rn = m.get("round_number", "?")
        for own_key, opp_key in (("player1_id", "player2_team"), ("player2_id", "player1_team")):
            uid = m.get(own_key)
            if not uid or uid not in member_ids:
                continue
            opp = html.escape(str(m.get(opp_key) or "Соперник"))
            debts_by_user.setdefault(uid, []).append(f"Тур {rn}: 🆚 <b>{opp}</b>")

    keyboard = [
        [InlineKeyboardButton("🔄 Разослать ещё раз", callback_data=f"admin_div_debts_dm:{div_id}")],
        [InlineKeyboardButton("« Назад", callback_data=f"admin_div_debts_menu:{div_id}")],
    ]

    if not debts_by_user:
        text = (
            f"✅ <b>Должников нет</b>\n\n"
            f"Дивизион: {html.escape(str(div_name))}\n"
            "Все просроченные матчи сыграны."
        )
        await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))
        return

    bar = "━━━━━━━━━━━━━━━━━━━━━━"
    cabinet_markup = InlineKeyboardMarkup(
        [[InlineKeyboardButton("📋 Мои матчи в кабинете", callback_data="cabinet_my_matches")]]
    )

    sent = 0
    for uid, debt_lines in debts_by_user.items():
        total = len(debt_lines)
        message = "\n".join([
            "🚨 <b>НАПОМИНАНИЕ О ЗАДОЛЖЕННОСТЯХ</b>\n",
            f"Дивизион: <b>{html.escape(str(div_name))}</b>",
            f"За вами <b>{total}</b> несыгранных матчей 🕒\n",
            bar,
            "⚽ <b>МАТЧИ ТУРНИРА</b>",
            *(f"   {i}. {line}" for i, line in enumerate(debt_lines, 1)),
            bar,
            "",
            "📅 Согласуйте время с соперниками и внесите результаты через кабинет — "
            "иначе последуют ⚠️ предупреждения!",
        ])
        # safe_send_notification гасит ошибку доставки каждого получателя отдельно,
        # так что заблокировавший бота игрок не обрывает рассылку остальным.
        if await safe_send_notification(context.bot, uid, message, cabinet_markup):
            sent += 1

    failed = len(debts_by_user) - sent
    text = (
        f"✉️ <b>Рассылка выполнена</b>\n\n"
        f"Дивизион: {html.escape(str(div_name))}\n"
        f"Должников: <b>{len(debts_by_user)}</b>\n"
        f"Доставлено: <b>{sent}</b>\n"
        f"Не доставлено: <b>{failed}</b>"
    )
    await _send_panel(update, context, text, InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_manage_players(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Участники своего дивизиона (выдача варнов) — без шага «Выберите дивизион»."""
    query = update.callback_query
    if not query or not query.data:
        return

    page = 0
    if query.data.startswith("admin_div_players:"):
        parts = query.data.split(":")
        div_id = _parse_div_arg(query, "admin_div_players")
        if len(parts) > 2:
            try:
                page = int(parts[2])
            except ValueError:
                page = 0
    else:
        div_id = _parse_div_arg(query, "admin_div_manage_players")

    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    if context.user_data is not None:
        context.user_data["admin_player_back_cb"] = f"admin_div_players:{div_id}:{page}"

    home_cb = _div_home_cb(update, div_id)
    back_btn_text = "« К дивизиону" if update.effective_user and is_global_admin(update.effective_user.id) else "« Назад в панель"

    page_players, div_title, total, page, total_pages = await _load_div_players_page(str(div_id), page)

    if not page_players:
        keyboard = [[InlineKeyboardButton(back_btn_text, callback_data=home_cb)]]
        await query.edit_message_text(f"👥 В «{html.escape(div_title)}» нет участников.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard = _div_player_buttons(page_players)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_div_players:{div_id}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_div_players:{div_id}:{page + 1}"))
    if nav_row:
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton(back_btn_text, callback_data=home_cb)])

    text = f"📋 <b>Участники: {html.escape(div_title)}</b> (Всего: {total}):\n\nВыберите игрока:"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Division Management Handlers ---

@admin_only
async def admin_divs_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Hub menu displaying all divisions with status and quick management actions."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return
    await query.answer()

    divisions = await asyncio.to_thread(database.get_divisions)
    keyboard = []

    for d in divisions:
        did = d["id"]
        users = await asyncio.to_thread(database.get_division_users, did)
        status_icon = "🟢" if d.get("is_active") else "🔴"
        keyboard.append([InlineKeyboardButton(f"{status_icon} {d['name']} ({len(users)} игр.)", callback_data=f"admin_div_view_{did}")])

    keyboard.append([InlineKeyboardButton("➕ Создать дивизион", callback_data="admin_div_create_start")])
    keyboard.append([InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")])

    text = (
        "🏆 <b>Дивизионы</b>\n\n"
        "Вся работа ведётся внутри дивизиона: матчи, составы, рассылка долгов, "
        "топики и участники — в карточке конкретного дивизиона.\n\n"
        "Выберите дивизион:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_view(update: Update, context: ContextTypes.DEFAULT_TYPE, div_id: int | None = None) -> None:
    """Detailed division card with settings and topic bindings."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return
    await query.answer()

    if div_id is None:
        target_raw = query.data.replace("admin_div_view_", "")
        div_id = int(target_raw)

    division = await asyncio.to_thread(database.get_division, div_id)
    if not division:
        keyboard = [[InlineKeyboardButton("« К списку дивизионов", callback_data="admin_divs_hub")]]
        await query.edit_message_text("❌ Дивизион не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    users = await asyncio.to_thread(database.get_division_users, div_id)
    topics_map = await asyncio.to_thread(database.get_division_topics_map, div_id)

    status_str = "🟢 Активен" if division.get("is_active") else "🔴 Отключен"
    # Raw message_thread_id values are meaningless to an admin and read like
    # counters next to "Участников", so show only whether a topic is bound.
    topic_lines = "\n".join(
        f"{database.TOPIC_DISPLAY_NAMES.get(topic_type, topic_type)} — "
        f"{'✅' if topics_map.get(topic_type, {}).get('message_thread_id') else '❌ не задан'}"
        for topic_type in database.PRIMARY_DIVISION_TOPICS
    )
    bound_count = sum(
        1 for topic_type in database.PRIMARY_DIVISION_TOPICS
        if topics_map.get(topic_type, {}).get("message_thread_id")
    )

    text = (
        f"🏆 <b>{html.escape(division['name'])}</b>\n\n"
        f"{status_str}  •  👥 Участников: {len(users)}\n\n"
        f"📌 <b>Топики группы</b> ({bound_count}/{len(database.PRIMARY_DIVISION_TOPICS)}):\n"
        f"{topic_lines}\n"
    )

    toggle_btn_text = "🔴 Отключить" if division.get("is_active") else "🟢 Включить"
    # Функциональные разделы идут первыми: карточка дивизиона — единственная
    # точка входа в матчи и составы, технические настройки ниже.
    keyboard = [
        [InlineKeyboardButton("⚔️ Управление матчами", callback_data=f"admin_div_manage_matches:{div_id}")],
        [
            InlineKeyboardButton("📋 Составы команд", callback_data=f"admin_roster_div:{div_id}"),
            InlineKeyboardButton("📊 Статус составов", callback_data=f"admin_squads_view:{div_id}"),
        ],
        [InlineKeyboardButton("🔗 Привязка клубов", callback_data=f"admin_bind_div:{div_id}")],
        [InlineKeyboardButton("📢 Рассылка задолженностей", callback_data=f"admin_div_debts_menu:{div_id}")],
        [
            InlineKeyboardButton(toggle_btn_text, callback_data=f"admin_div_toggle_{div_id}"),
            InlineKeyboardButton("✏️ Переименовать", callback_data=f"admin_div_rename_{div_id}")
        ],
        [
            InlineKeyboardButton("📌 Настроить топики", callback_data=f"admin_div_topics_{div_id}"),
            InlineKeyboardButton("👥 Участники", callback_data=f"admin_div_players:{div_id}:0")
        ],
        [InlineKeyboardButton("« К списку дивизионов", callback_data="admin_divs_hub")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle division active status."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return

    div_id = int(query.data.replace("admin_div_toggle_", ""))
    division = await asyncio.to_thread(database.get_division, div_id)
    if not division:
        await query.answer("❌ Дивизион не найден.", show_alert=True)
        return

    new_active = 0 if division.get("is_active") else 1
    await asyncio.to_thread(database.update_division, div_id, is_active=new_active)
    await query.answer(f"✅ Дивизион {'активирован' if new_active else 'деактивирован'}!", show_alert=False)
    await admin_div_view(update, context, div_id=div_id)


@admin_only
async def admin_div_topics_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Menu to manage topic bindings for a division."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return
    await query.answer()

    div_id = int(query.data.replace("admin_div_topics_", ""))
    division = await asyncio.to_thread(database.get_division, div_id)
    if not division:
        await query.edit_message_text("❌ Дивизион не найден.")
        return

    topics_map = await asyncio.to_thread(database.get_division_topics_map, div_id)

    keyboard = []
    for topic_type in database.PRIMARY_DIVISION_TOPICS:
        tid = topics_map.get(topic_type, {}).get("message_thread_id")
        label = database.TOPIC_DISPLAY_NAMES.get(topic_type, topic_type)
        keyboard.append([InlineKeyboardButton(
            f"{label}: {tid or 'Общий'}",
            callback_data=f"admin_div_settopic_{div_id}_{topic_type}"
        )])
    keyboard.append([InlineKeyboardButton("« К дивизиону", callback_data=f"admin_div_view_{div_id}")])

    types_hint = "|".join(database.PRIMARY_DIVISION_TOPICS)
    text = (
        f"📌 <b>Настройка тем для «{html.escape(division['name'])}»</b>\n\n"
        f"Нажмите на нужный тип топика, чтобы привязать числовой ID темы (message_thread_id) или сбросить на общий топик.\n\n"
        f"<i>💡 Вы также можете отправить команду <code>/set_div_topic {division['code']} [{types_hint}]</code> прямо внутри нужного топика группы!</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_div_create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start division creation conversation."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return ConversationHandler.END
    await query.answer()

    keyboard = [[InlineKeyboardButton("« Отмена", callback_data="admin_divs_hub")]]
    text = (
        "➕ <b>Создание нового дивизиона</b>\n\n"
        "Отправьте в чат название нового дивизиона.\n"
        "<i>Например: Премьер-Лига, Первый Дивизион, Кубок Надежды</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_EXPECT_DIV_NAME


# Транслитерация для кода дивизиона. Практическая, не ГОСТ: код читает человек
# и набирает его в /set_div_topic, так что важнее короткое и узнаваемое.
_CYRILLIC_TO_LATIN = {
    "а": "A", "б": "B", "в": "V", "г": "G", "д": "D", "е": "E", "ё": "E",
    "ж": "ZH", "з": "Z", "и": "I", "й": "Y", "к": "K", "л": "L", "м": "M",
    "н": "N", "о": "O", "п": "P", "р": "R", "с": "S", "т": "T", "у": "U",
    "ф": "F", "х": "KH", "ц": "TS", "ч": "CH", "ш": "SH", "щ": "SCH",
    "ъ": "", "ы": "Y", "ь": "", "э": "E", "ю": "YU", "я": "YA",
}


def _division_code_from_name(name: str) -> str:
    """Код дивизиона из названия: «Дивизион 6» → `DIVIZION6`.

    Кириллицу транслитерируем, а не выбрасываем. Отбрасывание оставляло от
    сплошь кириллического названия пустую строку, и код становился случайным
    (`DIV_7F3A`) — а по коду дивизиона ищется и сезонный состав клубов
    (`config.DIVISION_CLUBS`), и палитра инфографики, так что случайный код
    означал дивизион без клубов и с дефолтными цветами.
    """
    latin = "".join(_CYRILLIC_TO_LATIN.get(char, char) for char in name.lower())
    cleaned = re.sub(r"[^a-zA-Z0-9]", "", latin).upper()
    if len(cleaned) < 3:
        # Название без букв и цифр вообще (одни эмодзи) — занумеровать нечем.
        cleaned = f"DIV_{uuid.uuid4().hex[:4].upper()}"
    return cleaned[:16]


@admin_only
async def admin_div_create_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new division name, generate code, and insert into DB."""
    user = update.effective_user
    if not user or not await _ensure_super_admin(update):
        return ConversationHandler.END

    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("❌ Название слишком короткое (минимум 2 символа). Попробуйте еще раз:")
        return ADMIN_EXPECT_DIV_NAME

    base_code = _division_code_from_name(name)
    code = base_code
    counter = 1
    while database.get_division_by_code(code) is not None:
        code = f"{base_code}_{counter}"
        counter += 1

    div_id = await asyncio.to_thread(database.create_division, name=name, code=code)

    keyboard = [
        [InlineKeyboardButton("🏆 Перейти к дивизиону", callback_data=f"admin_div_view_{div_id}")],
        [InlineKeyboardButton("« К списку дивизионов", callback_data="admin_divs_hub")]
    ]
    await update.message.reply_text(
        f"✅ <b>Дивизион «{html.escape(name)}» успешно создан!</b>\n\n"
        f"• <b>Код:</b> <code>{code}</code>\n"
        f"• <b>ID:</b> <code>{div_id}</code>\n\n"
        f"Теперь вы можете привязать к нему участников и настроить форум-топики.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


@admin_only
async def admin_div_rename_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start division rename conversation."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return ConversationHandler.END
    await query.answer()

    div_id = int(query.data.replace("admin_div_rename_", ""))
    context.user_data["rename_div_id"] = div_id

    keyboard = [[InlineKeyboardButton("« Отмена", callback_data=f"admin_div_view_{div_id}")]]
    await query.edit_message_text(
        "✏️ <b>Переименование дивизиона</b>\n\n"
        "Отправьте новое название для дивизиона:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_EXPECT_DIV_RENAME


@admin_only
async def admin_div_rename_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive new name and update division in DB."""
    user = update.effective_user
    if not user or not await _ensure_super_admin(update):
        return ConversationHandler.END

    div_id = context.user_data.get("rename_div_id")
    if not div_id:
        await update.message.reply_text("❌ Ошибка: не найден дивизион.")
        return ConversationHandler.END

    new_name = update.message.text.strip()
    if len(new_name) < 2:
        await update.message.reply_text("❌ Название слишком короткое. Введите другое:")
        return ADMIN_EXPECT_DIV_RENAME

    await asyncio.to_thread(database.update_division, div_id, name=new_name)
    keyboard = [[InlineKeyboardButton("« К дивизиону", callback_data=f"admin_div_view_{div_id}")],
                [InlineKeyboardButton("« К списку дивизионов", callback_data="admin_divs_hub")]]
    await update.message.reply_text(
        f"✅ Название дивизиона изменено на: <b>{html.escape(new_name)}</b>!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END


@admin_only
async def admin_div_settopic_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Prompt for topic ID input."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return ConversationHandler.END
    await query.answer()

    parts = query.data.replace("admin_div_settopic_", "").rsplit("_", 1)
    div_id = int(parts[0])
    topic_type = parts[1]

    context.user_data["div_topic_div_id"] = div_id
    context.user_data["div_topic_type"] = topic_type

    label = database.TOPIC_DISPLAY_NAMES.get(database.normalize_topic_type(topic_type), topic_type)
    keyboard = [[InlineKeyboardButton("« Отмена", callback_data=f"admin_div_topics_{div_id}")]]
    await query.edit_message_text(
        f"📌 <b>Привязка топика «{label}»</b>\n\n"
        f"Отправьте числовой ID темы в супергруппе (<code>message_thread_id</code>).\n\n"
        f"• Отправьте <code>0</code> или <code>none</code>, чтобы сбросить тему на глобальную по умолчанию.\n"
        f"• Или напишите в нужном топике команду <code>/set_div_topic {div_id} {topic_type}</code>.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_EXPECT_DIV_TOPIC_ID


@admin_only
async def admin_div_settopic_receive(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive topic ID and bind in database."""
    user = update.effective_user
    if not user or not await _ensure_super_admin(update):
        return ConversationHandler.END

    div_id = context.user_data.get("div_topic_div_id")
    topic_type = context.user_data.get("div_topic_type")
    if not div_id or not topic_type:
        await update.message.reply_text("❌ Ошибка сессии настройки топика.")
        return ConversationHandler.END

    from services.topic_cache import topic_cache

    val = update.message.text.strip().lower()
    if val in ("0", "none", "нет", "сброс"):
        old = topic_cache.get_by_division(div_id, topic_type)
        await asyncio.to_thread(database.set_division_topic, div_id, topic_type, None)
        if old and old.get("group_chat_id") and old.get("message_thread_id"):
            topic_cache.remove_topic(int(old["group_chat_id"]), int(old["message_thread_id"]))
        msg = f"✅ Топик «{topic_type}» сброшен на глобальный по умолчанию."
    else:
        try:
            tid = int(val)
        except ValueError:
            await update.message.reply_text("❌ Введите корректный числовой ID топика (или 0 для сброса):")
            return ADMIN_EXPECT_DIV_TOPIC_ID

        # Голый thread_id без чата нероутируем: и topic_cache.get_by_topic, и
        # database.get_division_by_topic ищут по паре (group_chat_id,
        # message_thread_id), поэтому строка с group_chat_id IS NULL не находится
        # никогда. Берём чат уже привязанного топика дивизиона, иначе — основную
        # группу лиги. Кэш обновляем точечно: иначе привязка мертва до рестарта.
        chat_id = await _resolve_division_group_chat(div_id)
        if chat_id is None:
            await update.message.reply_text(
                "❌ Не удалось определить группу дивизиона. Привяжите топик командой "
                "<code>/set_div_topic</code> прямо внутри нужного топика супергруппы.",
                parse_mode="HTML"
            )
            return ConversationHandler.END

        await asyncio.to_thread(database.set_division_topic, div_id, topic_type, tid, chat_id)
        division = await asyncio.to_thread(database.get_division, div_id)
        topic_cache.set_topic(
            division_id=div_id,
            group_chat_id=chat_id,
            message_thread_id=tid,
            topic_type=topic_type,
            division_name=(division or {}).get("name", ""),
            division_code=(division or {}).get("code", "")
        )
        msg = f"✅ Топик «{topic_type}» успешно установлен на ID: <code>{tid}</code>!"

    keyboard = [[InlineKeyboardButton("« К настройке топиков", callback_data=f"admin_div_topics_{div_id}")],
                [InlineKeyboardButton("« К дивизиону", callback_data=f"admin_div_view_{div_id}")]]
    await update.message.reply_text(msg, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


@admin_only
async def admin_cancel_div_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel division conversation action."""
    if update.callback_query:
        await update.callback_query.answer()
        await admin_divs_hub(update, context)
    elif update.message:
        await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END


@admin_only
async def admin_set_div_topic_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /set_div_topic [div_id_or_code] [topic_type] called inside a group topic."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Доступ запрещён.")
        return

    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри нужного форум-топика супергруппы!")
        return

    types_hint = "|".join(database.PRIMARY_DIVISION_TOPICS)
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            f"⚠️ <b>Использование:</b> <code>/set_div_topic [ID_или_КОД_дивизиона] [{types_hint}]</code>\n\n"
            "Пример: <code>/set_div_topic 1 draft</code>",
            parse_mode="HTML"
        )
        return

    target = args[0].strip()
    # normalize_topic_type also accepts the Russian and plural aliases
    # ("черновик", "drafts", "составы", ...) listed in CANONICAL_TOPIC_TYPES.
    topic_type = database.normalize_topic_type(args[1])

    if topic_type not in database.PRIMARY_DIVISION_TOPICS:
        await update.message.reply_text(f"❌ Неверный тип топика. Разрешены: <code>{types_hint}</code>.", parse_mode="HTML")
        return

    division = None
    if target.isdigit():
        division = await asyncio.to_thread(database.get_division, int(target))
    if not division:
        division = await asyncio.to_thread(database.get_division_by_code, target)

    if not division:
        await update.message.reply_text(f"❌ Дивизион «{target}» не найден в базе данных.")
        return

    div_id = division["id"]
    # Привязку топика дивизиона делает супер-админ или админ этого дивизиона:
    # is_admin() выше истинен для админа ЛЮБОГО дивизиона.
    if not is_global_admin(user.id) and not await asyncio.to_thread(database.is_division_admin, user.id, div_id):
        await update.message.reply_text("⛔ У вас нет прав на этот дивизион.")
        return

    # group_chat_id обязателен: без него строка division_topics не находится ни
    # через topic_cache.get_by_topic, ни через get_division_by_topic, и привязка
    # остаётся нерабочей. Кэш обновляем точечно — иначе он живёт до рестарта.
    chat_id = update.effective_chat.id
    await asyncio.to_thread(database.set_division_topic, div_id, topic_type, thread_id, chat_id)

    from services.topic_cache import topic_cache
    topic_cache.set_topic(
        division_id=div_id,
        group_chat_id=chat_id,
        message_thread_id=thread_id,
        topic_type=topic_type,
        division_name=division.get("name", ""),
        division_code=division.get("code", "")
    )

    await update.message.reply_text(
        f"✅ Тема «{topic_type}» для дивизиона <b>{html.escape(division['name'])}</b> успешно привязана к этому топику (ID: <code>{thread_id}</code>)!",
        parse_mode="HTML"
    )

@admin_only
async def admin_confirm_delete_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ask admin for confirmation to delete the player."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    player_id = int(query.data.replace("admin_confirm_delete_player_", ""))
    player = await asyncio.to_thread(database.get_user, player_id)
    
    if not player:
        keyboard = [[InlineKeyboardButton("« Назад к списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
        
    text = (
        f"⚠️ <b>Подтвердите удаление</b>\n\n"
        f"Вы действительно хотите исключить игрока @{html.escape(str(player['username']))} "
        f"из лиги?\n\n"
        f"Клуб освободится для нового участника. Сыгранные матчи останутся в истории лиги, "
        f"несыгранные — в расписании."
    )
    # Тот же экран ведёт в admin_delete_player_execute, что и admin_delete_player_confirm,
    # поэтому и предупреждение про группу должно быть тем же.
    if player["division_id"]:
        text += "\n\n🚪 Игрок будет объявлен выбывшим в своём дивизионе и удалён из группы."
    keyboard = [
        [InlineKeyboardButton("🗑️ Да, удалить игрока", callback_data=f"admin_delete_player_execute_{player_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{player_id}")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")


@admin_only
async def admin_toggle_round_bets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Open/close the Logovo.bet line for a round independently of `is_open`.

    Позволяет выставить линию заранее: участники ставят прогнозы на тур,
    который ещё не открыт для внесения результатов.
    """
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    opening = query.data.startswith("admin_div_bets_open:")
    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return

    ok = await asyncio.to_thread(database.set_round_bets_open, round_number, opening, div_id)

    if ok and opening:
        await query.answer(f"🎰 Линия на Тур {round_number} открыта", show_alert=True)
        try:
            from services.betting_notifications import notify_division_betting_line_opened
            await notify_division_betting_line_opened(context, div_id, round_number)
        except Exception as e:
            logger.warning(f"Failed to send betting line opened notification: {e}")
    elif ok:
        await query.answer(f"🚫 Линия на Тур {round_number} закрыта", show_alert=True)
        try:
            from services.betting_notifications import notify_division_betting_line_closed
            await notify_division_betting_line_closed(context, div_id, round_number, was_open=True)
        except Exception as e:
            logger.warning(f"Failed to send betting line closed notification: {e}")
    else:
        await query.answer(
            f"❌ Не удалось открыть линию на Тур {round_number}: нет матчей или сезон неактивен.",
            show_alert=True
        )
        return

    await _render_div_round_card(query, context, div_id, round_number)


@admin_only
async def admin_open_preseason_line(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выставить предсезонную линию БК на Туры 1 и 2 дивизиона вручную."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    try:
        div_id = int(query.data.split(":")[1])
    except (IndexError, ValueError):
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return

    opened = await _open_preseason_line(div_id)
    if opened:
        try:
            from services.betting_notifications import notify_division_betting_line_opened
            for r_num in opened:
                await notify_division_betting_line_opened(context, div_id, r_num)
        except Exception as e:
            logger.warning(f"Failed to send preseason betting line notification: {e}")
        await query.answer(
            f"🎰 Линия открыта на Туры: {', '.join(str(r) for r in opened)}",
            show_alert=True
        )
    else:
        await query.answer(
            "❌ Не удалось открыть линию: туры уже открыты для игры, нет расписания или сезон неактивен.",
            show_alert=True
        )
        return

    await _render_div_round_card(query, context, div_id, opened[0])


@admin_only
async def admin_extend_match_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle deadline extension / freeze auto-warns for an overdue match."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return
    await query.answer()
    
    match_id = int(query.data.replace("admin_extend_match_", ""))
    new_val = await asyncio.to_thread(database.extend_match_deadline, match_id)
    
    if new_val == 1:
        await query.answer("⏸ Дедлайн продлен (авто-варны заморожены)", show_alert=True)
    else:
        await query.answer("▶️ Продление снято (авто-варны возобновлены)", show_alert=True)

    await admin_view_match(update, context, match_id=match_id)


@admin_only
async def admin_extend_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unfold the extension choices (+24ч / +48ч) for a debt match."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    match_id = int(query.data.replace("admin_extend_menu_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await safe_edit_or_reply(query, context, "❌ Матч не найден.")
        return
    if not await _ensure_match_access(update, match):
        return

    t1 = html.escape(match.get("player1_team") or "Хозяева")
    t2 = html.escape(match.get("player2_team") or "Гости")
    text = (
        f"⏸ <b>Продление матча #{match_id}</b>\n\n"
        f"🏠 <b>{t1}</b> 🆚 ✈️ <b>{t2}</b> (тур {match.get('round_number', '?')})\n\n"
        f"<i>На выбранный срок долг замораживается: напоминания и вердикт не срабатывают, "
        f"а ставки на матч продолжают висеть в статусе «в игре» и не возвращаются.</i>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ +24 часа", callback_data=f"admin_extend_24h_{match_id}")],
        [InlineKeyboardButton("➕ +48 часов", callback_data=f"admin_extend_48h_{match_id}")],
        [InlineKeyboardButton("« Назад к карточке матча", callback_data=f"admin_view_match_{match_id}")],
    ])
    await safe_edit_or_reply(query, context, text, reply_markup=keyboard)


@admin_only
async def admin_extend_hours_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Grant a debt match a fixed +24h / +48h extension and tell both players.

    Bets stay `pending` for the whole extension — nothing is refunded here,
    only a technical result (ТП / ТН) ever voids them.
    """
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    data = query.data
    hours = 24 if data.startswith("admin_extend_24h_") else 48
    match_id = int(data.replace(f"admin_extend_{hours}h_", ""))

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await safe_edit_or_reply(query, context, "❌ Матч не найден.")
        return
    if not await _ensure_match_access(update, match):
        return

    until_str = await asyncio.to_thread(database.extend_match_deadline_by_hours, match_id, hours)
    if not until_str:
        await query.answer("❌ Не удалось продлить матч.", show_alert=True)
        return

    until_dt = database.parse_flexible_datetime(until_str)
    until_human = until_dt.strftime("%d.%m.%Y %H:%M") if until_dt else until_str

    rn = match.get("round_number", "?")
    t1 = html.escape(match.get("player1_team") or "Хозяева")
    t2 = html.escape(match.get("player2_team") or "Гости")
    dm = (
        f"⏸ <b>Администратор продлил ваш матч-долг на {hours} часов.</b>\n\n"
        f"🏆 <b>{rn}-й тур:</b> 🏠 <b>{t1}</b> 🆚 ✈️ <b>{t2}</b>\n"
        f"🗓 Новый срок: <b>до {until_human}</b>\n\n"
        f"<i>Варны и вердикт на это время заморожены. Сыграйте матч и внесите результат — "
        f"это спишет варн за долг.</i>\n"
        f"💰 <i>Ставки на матч остаются в игре и не возвращаются.</i>"
    )
    for p_id in (match.get("player1_id"), match.get("player2_id")):
        if not p_id:
            continue
        try:
            await context.bot.send_message(chat_id=p_id, text=dm, parse_mode="HTML")
        except Exception:
            pass

    await query.answer(f"⏸ Матч продлён на {hours}ч (до {until_human})", show_alert=True)
    await admin_view_match(update, context, match_id=match_id)


@admin_only
async def admin_list_overdue(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Просроченные матчи одного дивизиона и быстрые действия по долгам."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return
    await query.answer()

    div_id = _parse_div_arg(query, "admin_div_overdue")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return
    if not await _ensure_division_access(update, div_id):
        return

    div = await asyncio.to_thread(database.get_division, div_id)
    div_name = div["name"] if div else f"#{div_id}"
    overdue_matches = await asyncio.to_thread(database.get_detailed_overdue_matches, div_id)

    keyboard = []
    if overdue_matches:
        for m in overdue_matches:
            rn = m.get("round_number", "?")
            t1 = m.get("player1_team") or m.get("p1_username") or "К1"
            t2 = m.get("player2_team") or m.get("p2_username") or "К2"
            hrs = int(m.get("hours_overdue", 0))
            ext_tag = " [⏸ Продлен]" if m.get("is_extended") else f" (⏳ {hrs}ч)"
            btn_text = f"Тур {rn}: {t1} vs {t2}{ext_tag}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_match_{m['id']}")])
        
        # Экран в скоупе дивизиона — и быстрые действия по долгам тоже, иначе
        # отсюда уходила бы рассылка по всем дивизионам сразу.
        keyboard.append([InlineKeyboardButton("📋 Сводка долгов в топик дивизиона", callback_data=f"admin_div_broadcast_debts:{div_id}")])
        keyboard.append([InlineKeyboardButton("✉️ ЛС должникам дивизиона", callback_data=f"admin_div_debts_dm:{div_id}")])
        text = (
            f"⏰ <b>Просроченные матчи — {html.escape(str(div_name))} ({len(overdue_matches)}):</b>\n\n"
            "Выберите матч для выставления счёта, ТП или индивидуального продления:"
        )
    else:
        text = (
            f"⏰ <b>Просроченных матчей-долгов в дивизионе {html.escape(str(div_name))} нет!</b>\n\n"
            "Все текущие матчи сыграны или дедлайны ещё не истекли."
        )

    keyboard.append([InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")])
    
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

async def _division_display_name(div_id: int) -> str:
    """Человекочитаемое название дивизиона с безопасным фолбэком."""
    div = await asyncio.to_thread(database.get_division, div_id)
    return (div or {}).get("name") or f"Дивизион {div_id}"


@admin_only
async def admin_open_round_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return ConversationHandler.END
    await query.answer()
    
    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return ConversationHandler.END
    if not await _ensure_division_access(update, div_id):
        return ConversationHandler.END

    # Расписание проверяется до запроса дедлайна: иначе админ вводит дату,
    # а отказ прилетает только на следующем шаге.
    if await asyncio.to_thread(database.count_round_matches, round_number, div_id) == 0:
        div_name = await _division_display_name(div_id)
        await query.edit_message_text(
            round_schedule_missing_message(round_number, div_name),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
            ),
        )
        return ConversationHandler.END

    # Лимит активных туров — второе предусловие, и проверяется тоже до запроса
    # дедлайна. Уже открытый тур собственный слот не занимает: смена дедлайна
    # такому туру разрешена, иначе его нельзя было бы продлить.
    round_info = await asyncio.to_thread(database.get_round_info, round_number, div_id)
    reopening = debt_policy.round_phase(round_info, now_msk()) == debt_policy.ROUND_CLOSED
    if reopening and not is_global_admin(query.from_user.id):
        await query.edit_message_text(
            "⛔ Тур уже закрыт, и его несыгранные матчи стали долгами. "
            "Переоткрыть закрытый тур может только глобальный админ.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("« К туру", callback_data=f"admin_div_round:{div_id}:{round_number}")]]
            ),
        )
        return ConversationHandler.END
    if not (round_info and round_info.get("is_open")):
        active = await asyncio.to_thread(database.get_active_open_rounds, div_id)
        if len(active) >= config.MAX_OPEN_ROUNDS_PER_DIVISION:
            await query.edit_message_text(
                max_active_rounds_message(active),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
                ),
            )
            return ConversationHandler.END

    context.user_data["admin_round_to_open"] = round_number
    context.user_data["admin_round_open_div"] = div_id

    keyboard = [[InlineKeyboardButton("Отмена", callback_data="admin_cancel_match_action")]]
    reopen_note = (
        "♻️ Тур будет переоткрыт: долги его матчей, по которым ещё не было "
        "вердикта, снимутся и начнутся заново от нового дедлайна.\n\n"
        if reopening else ""
    )
    await query.edit_message_text(
        f"{reopen_note}Укажите строгий дедлайн для {round_number}-го тура.\n"
        "Формат: `ДД.ММ.ГГГГ ЧЧ:ММ` (например: `29.07.2026 23:59`)\n"
        "Дедлайн обязателен и должен быть в будущем.\n\n"
        "Отправьте текст дедлайна:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_WAITING_FOR_DEADLINE

import datetime

async def admin_open_round_save(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return ConversationHandler.END

    if not update.message or not update.message.text:
        return ADMIN_WAITING_FOR_DEADLINE
        
    deadline_text = update.message.text.strip()

    try:
        database.validate_round_deadline(deadline_text)
    except database.RoundDeadlineError as e:
        await update.message.reply_text(f"❌ {e.reason}\nОтправьте дедлайн ещё раз (ДД.ММ.ГГГГ ЧЧ:ММ).")
        return ADMIN_WAITING_FOR_DEADLINE

    round_number = context.user_data.pop("admin_round_to_open", None)
    div_id = context.user_data.pop("admin_round_open_div", None)
    if not round_number or not div_id:
        return ConversationHandler.END

    # Гейт расписания стоит и здесь, а не только в prompt: между запросом
    # дедлайна и вводом ответа матчи тура могли быть удалены.
    r_info = await asyncio.to_thread(database.get_round_info, round_number, div_id)
    was_bets_open = bool(r_info and r_info.get("bets_open"))
    prev_phase = debt_policy.round_phase(r_info, now_msk())
    # Тур мог закрыться, пока админ набирал дату.
    if prev_phase == debt_policy.ROUND_CLOSED and not is_global_admin(user.id):
        await update.message.reply_text("⛔ Тур уже закрыт. Переоткрыть его может только глобальный админ.")
        return ConversationHandler.END

    try:
        advanced = await asyncio.to_thread(
            database.update_round_status, round_number, is_open=True, deadline=deadline_text, division_id=div_id
        )
    except database.RoundScheduleMissingError:
        div_name = await _division_display_name(div_id)
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await update.message.reply_text(
            round_schedule_missing_message(round_number, div_name),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END
    except database.MaxActiveRoundsExceededError:
        active = await asyncio.to_thread(database.get_active_open_rounds, div_id)
        keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
        await update.message.reply_text(
            max_active_rounds_message(active),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    if prev_phase in (debt_policy.ROUND_OPEN, debt_policy.ROUND_OVERDUE):
        headline = f"🕒 <b>Дедлайн {round_number}-го тура изменён</b>"
    elif prev_phase == debt_policy.ROUND_CLOSED:
        headline = f"♻️ <b>{round_number}-й Тур переоткрыт!</b>"
    else:
        headline = f"🟢 <b>Открыт {round_number}-й Тур!</b>"
    announced = await _announce_rounds_opened(
        context,
        div_id,
        f"{headline}\n\n🕒 Дедлайн: {html.escape(deadline_text)}\n\n"
        "Пожалуйста, сыграйте свои матчи и внесите результаты до истечения срока.",
        include_table=(round_number == 1 and prev_phase == debt_policy.ROUND_SCHEDULED),
    )

    notice = (
        "Уведомление отправлено в топик «📞 ОТЧЁТЫ» дивизиона и игрокам в ЛС!"
        if announced
        else "⚠️ Топик «📞 ОТЧЁТЫ» у дивизиона не настроен — объявление в группу не отправлено. Игроки уведомлены в ЛС."
    )
    keyboard = [[InlineKeyboardButton("« К туру", callback_data=f"admin_div_round:{div_id}:{round_number}")]]
    await update.message.reply_text(
        f"✅ {round_number}-й тур успешно открыт. Строгий дедлайн: {deadline_text}\n{notice}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await notify_players_rounds_opened(context, [round_number], deadline_text, division_id=div_id)

    if was_bets_open:
        try:
            from services.betting_notifications import notify_division_betting_line_closed
            await notify_division_betting_line_closed(context, div_id, round_number, was_open=True)
        except Exception as e:
            logger.warning(f"Failed to notify betting line closed for round {round_number}: {e}")

    for adv_r in (advanced or []):
        try:
            from services.betting_notifications import notify_division_betting_line_opened
            await notify_division_betting_line_opened(context, div_id, adv_r)
        except Exception as e:
            logger.warning(f"Failed to notify betting line opened for advanced round {adv_r}: {e}")
    return ConversationHandler.END

@admin_only
async def admin_open_batch_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return ConversationHandler.END
    await query.answer()

    div_id = _parse_div_arg(query, "admin_batch_open_div")
    if div_id is None:
        await _deny_access(update, "⛔ Дивизион не определён")
        return ConversationHandler.END
    if not await _ensure_division_access(update, div_id):
        return ConversationHandler.END
    context.user_data["batch_div_id"] = div_id

    back_keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]
    )

    # Диапазон админ больше не вводит: туры идут строго парами, и какая пара
    # следующая — вопрос к БД, а не к админу. Сначала лимит, потом подбор пары.
    active = await asyncio.to_thread(database.get_active_open_rounds, div_id)
    if len(active) >= config.MAX_OPEN_ROUNDS_PER_DIVISION:
        await query.edit_message_text(
            max_active_rounds_message(active),
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return ConversationHandler.END

    free_slots = config.MAX_OPEN_ROUNDS_PER_DIVISION - len(active)
    next_rounds = await asyncio.to_thread(database.get_next_rounds_to_open, div_id, None, free_slots)
    div_name = await _division_display_name(div_id)
    if not next_rounds:
        await query.edit_message_text(
            f"❌ Нельзя открыть следующие туры — {html.escape(div_name)}: "
            "расписание ещё не сгенерировано. Сначала создайте матчи через меню админа.",
            parse_mode="HTML",
            reply_markup=back_keyboard,
        )
        return ConversationHandler.END

    start_r, end_r = next_rounds[0], next_rounds[-1]
    context.user_data["batch_start"] = start_r
    context.user_data["batch_end"] = end_r

    rounds_label = f"{start_r} и {end_r}" if start_r != end_r else str(start_r)
    keyboard = [[InlineKeyboardButton("Отмена", callback_data="admin_cancel_match_action")]]
    await query.edit_message_text(
        f"📅 <b>Открытие туров {rounds_label} ({html.escape(div_name)})</b>\n\n"
        "Укажите дедлайн (ДД.ММ.ГГГГ ЧЧ:ММ):",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ADMIN_WAITING_FOR_BATCH_DEADLINE

async def admin_open_batch_deadline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    if not user or not is_admin(user.id):
        return ConversationHandler.END

    if not update.message or not update.message.text:
        return ADMIN_WAITING_FOR_BATCH_DEADLINE
        
    deadline_text = update.message.text.strip()
    try:
        database.validate_round_deadline(deadline_text)
    except database.RoundDeadlineError as e:
        await update.message.reply_text(f"❌ {e.reason}\nОтправьте дедлайн ещё раз (ДД.ММ.ГГГГ ЧЧ:ММ).")
        return ADMIN_WAITING_FOR_BATCH_DEADLINE

    start_r = context.user_data.pop("batch_start", None)
    end_r = context.user_data.pop("batch_end", None)
    div_id = context.user_data.pop("batch_div_id", None)
    if not start_r or not end_r or not div_id:
        return ConversationHandler.END

    keyboard = [[InlineKeyboardButton("« К турам", callback_data=f"admin_div_manage_matches:{div_id}")]]

    # Туры без расписания пачка не открывает — они возвращаются в `skipped`.
    # Лимит проверяется и здесь, а не только в prompt: пока админ набирал дату,
    # туры мог открыть другой админ или текстовая команда.
    rounds_with_bets_open = []
    for r_num in range(start_r, end_r + 1):
        r_info = await asyncio.to_thread(database.get_round_info, r_num, div_id)
        if r_info and r_info.get("bets_open"):
            rounds_with_bets_open.append(r_num)

    try:
        report = await asyncio.to_thread(database.open_rounds_batch, start_r, end_r, deadline_text, division_id=div_id)
    except database.MaxActiveRoundsExceededError:
        active = await asyncio.to_thread(database.get_active_open_rounds, div_id)
        await update.message.reply_text(
            max_active_rounds_message(active),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    opened_rounds = report.get("opened", [])
    skipped_rounds = report.get("skipped", [])

    if not opened_rounds:
        div_name = await _division_display_name(div_id)
        if len(skipped_rounds) == 1:
            text = round_schedule_missing_message(skipped_rounds[0], div_name)
        else:
            text = (
                f"❌ Нельзя открыть туры {', '.join(str(r) for r in skipped_rounds)} — "
                f"{html.escape(div_name)}: расписание ещё не сгенерировано. "
                "Сначала создайте матчи через меню админа."
            )
        await update.message.reply_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return ConversationHandler.END

    # Перечисляем открытые туры списком, а не диапазоном: в диапазоне мог
    # оказаться пропущенный тур без расписания.
    opened_list = ", ".join(str(r) for r in opened_rounds)
    announced = await _announce_rounds_opened(
        context,
        div_id,
        f"🟢 <b>Открыты туры: {opened_list}!</b>\n\n🕒 Дедлайн: {html.escape(deadline_text)}\n\n"
        "Пожалуйста, сыграйте свои матчи и внесите результаты до истечения срока.",
        include_table=(1 in opened_rounds),
    )

    notice = (
        "Уведомления отправлены в топик «📞 ОТЧЁТЫ» дивизиона и игрокам в ЛС!"
        if announced
        else "⚠️ Топик «📞 ОТЧЁТЫ» у дивизиона не настроен — объявление в группу не отправлено. Игроки уведомлены в ЛС."
    )
    skipped_notice = (
        f"\n⚠️ Пропущены туры без расписания: {', '.join(str(r) for r in skipped_rounds)}. "
        "Сначала создайте для них матчи через меню админа."
        if skipped_rounds
        else ""
    )
    await update.message.reply_text(
        f"✅ Открыты туры: {opened_list}.\n"
        f"Дедлайн: {deadline_text}{skipped_notice}\n{notice}",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    await notify_players_rounds_opened(context, opened_rounds, deadline_text, division_id=div_id)

    for r_num in opened_rounds:
        if r_num in rounds_with_bets_open:
            try:
                from services.betting_notifications import notify_division_betting_line_closed
                await notify_division_betting_line_closed(context, div_id, r_num, was_open=True)
            except Exception as e:
                logger.warning(f"Failed to notify betting line closed for batch round {r_num}: {e}")

    for adv_r in report.get("advanced", []):
        try:
            from services.betting_notifications import notify_division_betting_line_opened
            await notify_division_betting_line_opened(context, div_id, adv_r)
        except Exception as e:
            logger.warning(f"Failed to notify betting line opened for batch advanced round {adv_r}: {e}")
    return ConversationHandler.END

@admin_only
async def admin_close_round(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return
    await query.answer()
    
    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return

    preview = await asyncio.to_thread(database.preview_close_round, round_number, div_id)
    keyboard = [
        [InlineKeyboardButton("🔴 Да, закрыть тур", callback_data=f"admin_div_round_close_ok:{div_id}:{round_number}")],
        [InlineKeyboardButton("« Отмена", callback_data=f"admin_div_round:{div_id}:{round_number}")],
    ]
    await query.edit_message_text(
        close_round_preview_text(preview),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


def _debt_term_line(early: bool, grace_hours: int, escalate_at) -> str:
    base = f"до <b>{_fmt_msk(escalate_at)}</b> ({config.DEBT_ESCALATION_HOURS} ч"
    if early and grace_hours:
        base += f" + {grace_hours} ч до дедлайна"
    return base + ")"


def close_round_preview_text(preview: dict) -> str:
    """Экран подтверждения закрытия: сколько матчей уйдёт в долг и до какого срока."""
    rn = preview["round_number"]
    pending = preview.get("matches") or []
    text = f"🔴 <b>Закрыть {rn}-й тур?</b>\n\n"
    if preview.get("status") == debt_policy.ROUND_CLOSED:
        text += "Тур уже закрыт — повторное закрытие ничего не меняет.\n"
        return text
    if not pending:
        text += "Все матчи тура сыграны — долгов не будет."
        return text
    text += f"В долг уйдут матчей: <b>{len(pending)}</b>.\n"
    text += "Срок отыгрыша " + _debt_term_line(preview["early"], preview["grace_hours"], preview["escalate_at"]) + ".\n"
    if preview.get("early"):
        text += "Тур закрывается раньше дедлайна: остаток времени до него добавлен к сроку.\n"
    text += "\nРезультаты этих матчей принимаются и после закрытия — как отыгрыш долга.\n\n"
    for m in pending[:15]:
        text += f"• {html.escape(str(m.get('player1_team') or '?'))} — {html.escape(str(m.get('player2_team') or '?'))}\n"
    if len(pending) > 15:
        text += f"… и ещё {len(pending) - 15}\n"
    return text


def _close_round_announcement(result: dict) -> str:
    rn = result["round_number"]
    debts = result.get("debts") or []
    text = f"🔴 <b>{rn}-й Тур закрыт.</b>\n\n"
    if not debts:
        return text + "Все матчи тура сыграны. Спасибо!"
    text += f"Несыгранные матчи ({len(debts)}) переходят в долг.\n"
    text += "Срок отыгрыша " + _debt_term_line(result["early"], result["grace_hours"], result["escalate_at"]) + ".\n\n"
    for m in debts:
        p1 = f"@{m['p1_username']}" if m.get("p1_username") else (m.get("player1_team") or "?")
        p2 = f"@{m['p2_username']}" if m.get("p2_username") else (m.get("player2_team") or "?")
        text += (
            f"• {html.escape(str(m.get('player1_team') or '?'))} ({html.escape(str(p1))}) — "
            f"{html.escape(str(m.get('player2_team') or '?'))} ({html.escape(str(p2))})\n"
        )
    return text


@admin_only
async def admin_close_round_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Закрыть тур после подтверждения: матчи — в долг, объявление, ЛС должникам."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    div_id, round_number = _parse_div_round_arg(query)
    if div_id is None or round_number is None:
        await _deny_access(update, "⛔ Некорректные данные")
        return
    if not await _ensure_division_access(update, div_id):
        return

    keyboard = [[InlineKeyboardButton("« К туру", callback_data=f"admin_div_round:{div_id}:{round_number}")]]
    info = await asyncio.to_thread(database.get_round_info, round_number, div_id)
    if debt_policy.round_phase(info, now_msk()) == debt_policy.ROUND_CLOSED:
        await query.edit_message_text(f"🔴 {round_number}-й тур уже закрыт.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    result = await asyncio.to_thread(database.close_round, round_number, div_id, query.from_user.id)
    debts = result.get("debts") or []

    announced = await _announce_rounds_opened(context, div_id, _close_round_announcement(result), include_table=False)

    term = _debt_term_line(result["early"], result["grace_hours"], result["escalate_at"])
    notified = 0
    for m in debts:
        for uid, team, opp in (
            (m.get("player1_id"), m.get("player1_team"), m.get("player2_team")),
            (m.get("player2_id"), m.get("player2_team"), m.get("player1_team")),
        ):
            if not uid:
                continue
            try:
                ok = await safe_send_notification(
                    context.bot, uid,
                    f"🔴 <b>{round_number}-й тур закрыт.</b>\n\n"
                    f"Ваш матч {html.escape(str(team or '?'))} — {html.escape(str(opp or '?'))} не сыгран "
                    f"и перешёл в долг.\nСрок отыгрыша {term}.\n"
                    "После срока матч уйдёт админам на технический вердикт.",
                )
                notified += 1 if ok else 0
            except Exception as e:
                logger.warning(f"Failed to notify debtor {uid} about closed round {round_number}: {e}")

    text = f"🔴 {round_number}-й тур закрыт.\n"
    if debts:
        text += f"Долгов: {len(debts)}, срок отыгрыша {term}.\nДолжникам отправлено сообщений: {notified}.\n"
    else:
        text += "Все матчи сыграны — долгов нет.\n"
    text += (
        "Объявление отправлено в «📞 ОТЧЁТЫ»."
        if announced
        else "⚠️ Топик «📞 ОТЧЁТЫ» у дивизиона не настроен — объявление в группу не отправлено."
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_round_matches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Display all matches in a round for admin action."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    try:
        await query.answer()
    except Exception:
        pass
    
    round_number = int(query.data.replace("admin_round_matches_", ""))
    matches = await asyncio.to_thread(database.get_matches_by_round, round_number)

    # Глобальный список тура — общий для всех дивизионов; админу дивизиона
    # показываем только его матчи, иначе он попадал в чужие карточки матчей.
    if not is_global_admin(query.from_user.id):
        allowed = {d["id"] for d in await asyncio.to_thread(database.get_admin_divisions, query.from_user.id)}
        if not allowed:
            await _deny_access(update, "⛔ У вас нет прав на этот дивизион")
            return
        matches = [m for m in matches if m.get("division_id") in allowed]

    keyboard = []
    for m in matches:
        opp1 = m["player1_nickname"]
        opp2 = m["player2_nickname"]
        
        if m["status"] == "confirmed":
            status_lbl = f"{m['player1_score']}:{m['player2_score']}"
        elif m["status"] == "disputed":
            status_lbl = "⚠️ спор"
        else:
            status_lbl = "⚔️"
            
        btn_text = f"{opp1} vs {opp2} ({status_lbl})"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_match_{m['id']}")])
        
    back_cb = _round_back_cb(context, round_number)
    keyboard.append([InlineKeyboardButton("« Назад к туру", callback_data=back_cb)])

    text = f"📅 **Матчи {round_number}-го тура (Панель Администратора):**\n\nВыберите матч для ввода счета или сброса:"
    target_chat_id = query.message.chat_id if query and query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

@admin_only
async def admin_view_match(update: Update, context: ContextTypes.DEFAULT_TYPE, match_id: int | None = None) -> None:
    """View details of a single match with admin actions."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    if match_id is None:
        match_id = int(query.data.replace("admin_view_match_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)
    
    if not match:
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="admin_main_menu")]]
        await query.edit_message_text("❌ Матч не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if not await _ensure_match_access(update, match):
        return

    # Дивизион берём из самого матча — карточка открывается и из списка тура,
    # и из просроченных, и из уведомлений, где контекста дивизиона нет.
    match_div_id = match.get("division_id")
    match_round = match.get("round_number")
    back_cb = (
        f"admin_div_round_matches:{match_div_id}:{match_round}"
        if match_div_id and match_round
        else "admin_main_menu"
    )

    header_title = f"⚽️ <b>Карточка матча #{match['id']} (Тур {match.get('round_number', '?')})</b>"
    back_button = InlineKeyboardButton("« Назад", callback_data=back_cb)

    status_map = {
        "pending": "⚔️ Ожидает игры",
        "confirmed": "✅ Завершен",
        "disputed": "⚠️ Оспорен (Спор)"
    }
    
    club1 = f" [{html.escape(match['player1_team'])}]" if match['player1_team'] else ""
    club2 = f" [{html.escape(match['player2_team'])}]" if match['player2_team'] else ""
    p1_name = html.escape(str(match['player1_nickname'] or match['player1_team'] or ""))
    p2_name = html.escape(str(match['player2_nickname'] or match['player2_team'] or ""))
    score_str = f"<code>{match['player1_score']} : {match['player2_score']}</code>" if match['player1_score'] is not None else "Не сыгран"
    
    is_overdue = await asyncio.to_thread(database.is_match_overdue, match_id)
    is_extended = bool(match.get("is_extended", 0))
    extra_status = ""
    if match.get("status") == "pending":
        if is_extended:
            until_dt = await asyncio.to_thread(database.get_match_extension_expiry, match_id)
            until_txt = f" до {until_dt.strftime('%d.%m.%Y %H:%M')}" if until_dt else ""
            extra_status = f"\n• <b>Отсчёт долга:</b> ⏸ <i>Заморожен (продлён админом{until_txt})</i>"
        elif is_overdue:
            extra_status = "\n• <b>Статус долга:</b> ⏳ <b>Матч-долг (48ч на отыгровку, далее ТП/ТН)</b>"

    text = (
        f"{header_title}\n\n"
        f"⚔️ <b>{p1_name}</b>{club1}\n"
        f" 🆚 <b>{p2_name}</b>{club2}\n\n"
        f"• <b>Текущий счет:</b> {score_str}\n"
        f"• <b>Статус:</b> {html.escape(status_map.get(match['status'], match['status']))}{extra_status}\n"
        f"📜 <a href=\"https://t.me/fifulatyrniru/3405\">Правила турнира</a>"
    )
    
    keyboard = [
        [InlineKeyboardButton("📜 Правила турнира", url="https://t.me/fifulatyrniru/3405")],
        [InlineKeyboardButton("⚡ Внести результат по фото (ИИ)", callback_data=f"admin_report_score_auto_{match_id}")],
        [InlineKeyboardButton("✍️ Внести результат вручную", callback_data=f"cb_report_choice_manual_{match_id}")],
        [InlineKeyboardButton("🚫 ТП 1:0 (Хозяева)", callback_data=f"admin_tp_home_{match_id}"), InlineKeyboardButton("🚫 ТП 0:1 (Гости)", callback_data=f"admin_tp_away_{match_id}")],
    ]
    if match.get("status") == "pending":
        if is_extended:
            keyboard.append([InlineKeyboardButton("▶️ Снять продление и возобновить отсчёт", callback_data=f"admin_extend_match_{match_id}")])
        else:
            keyboard.append([InlineKeyboardButton("⏸ Продлить матч: +24ч / +48ч", callback_data=f"admin_extend_menu_{match_id}")])
    keyboard.append([InlineKeyboardButton("🤝 ТН 0:0 (Ничья)", callback_data=f"admin_tp_draw_{match_id}"), InlineKeyboardButton("🔄 Сбросить результат", callback_data=f"admin_reset_match_execute_{match_id}")])
    if match.get("photo_id"):
        keyboard.append([InlineKeyboardButton("📸 Просмотр скриншота матча", callback_data=f"admin_view_match_photo_{match_id}")])
    keyboard.append([back_button])

    target_chat_id = query.message.chat_id if query and query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

@admin_only
async def admin_view_match_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the uploaded screenshot of a match to the admin."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    match_id = int(query.data.replace("admin_view_match_photo_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)

    if not match:
        await safe_edit_or_reply(query, context, "❌ Матч не найден.")
        return

    if not await _ensure_match_access(update, match):
        return

    photo_id = match.get("photo_id")
    if not photo_id:
        await safe_edit_or_reply(query, context, "📸 Скриншот для этого матча не был загружен.")
        return

    p1 = html.escape(str(match['player1_nickname'] or match['player1_team'] or ""))
    p2 = html.escape(str(match['player2_nickname'] or match['player2_team'] or ""))
    score_str = f"{match['player1_score']} : {match['player2_score']}" if match['player1_score'] is not None else "Не сыгран"
    title = f"Тур {match.get('round_number', '?')}"

    caption = (
        f"📸 <b>Скриншот матча #{match_id} ({title})</b>\n"
        f"⚔️ <b>{p1}</b> {score_str} <b>{p2}</b>"
    )
    back_button = InlineKeyboardMarkup([[InlineKeyboardButton("« Назад к карточке матча", callback_data=f"admin_view_match_{match_id}")]])

    target_chat_id = query.message.chat_id if query and query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    try:
        await context.bot.send_photo(chat_id=target_chat_id, message_thread_id=thread_id, photo=photo_id, caption=caption, parse_mode="HTML", reply_markup=back_button)
    except BadRequest as e:
        logger.warning(f"Failed to resend screenshot for match #{match_id}: {e}")
        await safe_edit_or_reply(query, context, "📸 Не удалось отобразить скриншот (файл недоступен).")

@admin_only
async def admin_report_score_auto(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start AI Vision photo recognition flow for Admin."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id): return
    await query.answer()

    match_id = int(query.data.replace("admin_report_score_auto_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await query.edit_message_text("❌ Матч не найден.")
        return

    context.user_data["reporting_match_id"] = match_id
    context.user_data["report_home_team"] = match['player1_team'] or match['player1_nickname']
    context.user_data["report_away_team"] = match['player2_team'] or match['player2_nickname']
    context.user_data["reporter_id"] = query.from_user.id
    context.user_data["reporting_mode"] = "auto"
    context.user_data["is_admin_reporting"] = True
    context.user_data["awaiting_report_photo"] = True
    context.user_data["ai_photos_list"] = []

    text = (
        f"🤖 <b>Автоматический ввод по фото (Администратор)</b>\n\n"
        f"Пожалуйста, отправьте <b>от 1 до 3 скриншотов</b> матча #{match_id} строго с статистикой (голы и ассисты).\n\n"
        f"💡 <i>ИИ мгновенно распознает счет, составы и предложит занести результат в лигу.</i>"
    )
    keyboard = [[InlineKeyboardButton("« Назад к карточке матча", callback_data=f"admin_view_match_{match_id}")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def _notify_group_about_tp(
    context: ContextTypes.DEFAULT_TYPE, match_id: int, tp_type: str, is_debt: bool = False
):
    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        return

    p1 = match.get("player1_nickname") or match.get("direct_p1_team") or "Хозяева"
    p2 = match.get("player2_nickname") or match.get("direct_p2_team") or "Гости"
    rnd = match.get("round_number", "?")
    tour_type = match.get("tournament_type", "league")
    
    tour_text = f"Тур {rnd}" if tour_type == "league" else "Кубковый матч"
    
    if tp_type == "home":
        res_text = f"{p1} <b>1:0</b> {p2} (ТП)"
    elif tp_type == "away":
        res_text = f"{p1} <b>0:1</b> {p2} (ТП)"
    else:
        res_text = f"{p1} <b>0:0</b> {p2} (ТН)"

    text = f"🚨 <b>Администратор назначил результат:</b>\n\n🏆 <b>{tour_text}</b>\n🎮 {res_text}"

    # Варны по ТП/ТН описывает отчёт в ПРЕДЫ; «сыгранный долг» здесь не к месту.
    if is_debt:
        text += "\n\n⚖️ <i>Матч был долгом — закрыт вердиктом администратора.</i>"

    # Determine target chat and topic strictly by division
    target_chat_id, target_topic_id = await resolve_division_target(
        match.get("division_id"), "reports", "results",
        legacy_topic_keys=("reports_topic_id",),
    )
    if not target_chat_id:
        return

    kwargs = {"chat_id": target_chat_id, "text": text, "parse_mode": "HTML"}
    if target_topic_id:
        kwargs["message_thread_id"] = int(target_topic_id)
        
    try:
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logger.error(f"Failed to send TP notification to group: {e}")

_VERDICT_ALERTS = {
    "home": "✅ Назначено ТП 1:0 (Победа Хозяев)",
    "away": "✅ Назначено ТП 0:1 (Победа Гостей)",
    "draw": "✅ Назначена Техническая ничья 0:0",
}


async def _report_technical_verdict(
    context: ContextTypes.DEFAULT_TYPE, match_id: int, verdict: str, outcome: dict
) -> None:
    """ЛС участникам, отчёт в ПРЕДЫ и автокик — по уже применённому вердикту."""
    m = await asyncio.to_thread(database.get_match, match_id)
    if not m:
        return
    rn = m.get("round_number", 0) or 0
    p1_id, p2_id = outcome.get("players") or (m.get("player1_id"), m.get("player2_id"))
    t1 = html.escape(m.get("player1_team") or "Хозяева")
    t2 = html.escape(m.get("player2_team") or "Гости")
    u1 = f"@{html.escape(m['player1_username'])}" if m.get("player1_username") else t1
    u2 = f"@{html.escape(m['player2_username'])}" if m.get("player2_username") else t2
    names = {p1_id: (u1, m.get("player1_username")), p2_id: (u2, m.get("player2_username"))}
    warned = outcome.get("warned") or []
    unwarned = outcome.get("unwarned") or []

    if verdict == "draw":
        score_line = "🤝 <b>ТН 0:0</b> — по 1 очку каждому"
    elif verdict == "home":
        score_line = f"🏆 <b>ТП 1:0</b> — победа {t1}"
    else:
        score_line = f"🏆 <b>ТП 0:1</b> — победа {t2}"

    for p_id in (p1_id, p2_id):
        if not p_id:
            continue
        personal = ""
        for uid, cnt in unwarned:
            if uid == p_id:
                personal = (
                    f"🎁 <b>С вас списан 1 варн за долг.</b>\n"
                    f"📊 Текущие варны: <b>{cnt}/{MAX_WARNS_LIMIT}</b>\n"
                )
        for uid, cnt in warned:
            if uid == p_id:
                personal = (
                    f"🚨 <b>Вам начислен +1 варн.</b>\n"
                    f"📊 Текущие варны: <b>{cnt}/{MAX_WARNS_LIMIT}</b>\n"
                )
        dm = (
            f"⚖️ <b>Вердикт по матчу-долгу</b>\n\n"
            f"🏆 <b>{rn}-й тур:</b> 🏠 <b>{t1}</b> ({u1}) 🆚 ✈️ <b>{t2}</b> ({u2})\n"
            f"{score_line}\n\n"
            f"{personal}"
            f"💰 <i>Все ставки на этот матч возвращены игрокам (кэф 1.00).</i>"
        )
        try:
            await context.bot.send_message(chat_id=p_id, text=dm, parse_mode="HTML")
        except Exception:
            pass

    lines = [
        "⚖️ <b>ДИСЦИПЛИНАРНЫЙ ВЕРДИКТ ПО ДОЛГУ</b>\n",
        f"🏆 Матч: {rn}-й тур — <b>{t1}</b> ({u1}) 🆚 <b>{t2}</b> ({u2})",
        score_line,
        "",
    ]
    for uid, cnt in warned:
        lines.append(f"🚨 {names.get(uid, (f'ID {uid}', None))[0]}: <b>+1 варн</b> → <b>{cnt}/{MAX_WARNS_LIMIT}</b>")
    for uid, cnt in unwarned:
        lines.append(
            f"🎁 {names.get(uid, (f'ID {uid}', None))[0]}: <b>−1 варн</b> за закрытие долга → <b>{cnt}/{MAX_WARNS_LIMIT}</b>"
        )
    lines.append("")
    lines.append("💰 <i>Все ставки на этот матч возвращены игрокам (Refund, кэф 1.00).</i>")
    await _send_to_warns_thread(context, "\n".join(lines), m.get("division_id"))

    # Автокик — только после отчёта, чтобы ветка ПРЕДЫ читалась по порядку.
    for p_id in outcome.get("kick") or []:
        uname = names.get(p_id, (None, None))[1]
        team = m.get("player1_team") if p_id == p1_id else m.get("player2_team")
        await _auto_kick_player(context, p_id, uname, team)


@admin_only
async def admin_set_technical_result_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """ТП хозяевам / ТП гостям / ТН — `admin_tp_(home|away|draw)_<id>`.

    Счёт, возврат ставок и варны применяет `database.apply_technical_verdict` одной
    транзакцией; варны — только если матч долг и вердикт по нему ещё не выносился.
    """
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    parsed = re.fullmatch(r"admin_tp_(home|away|draw)_(\d+)", query.data or "")
    if not parsed:
        await query.answer()
        return
    verdict, match_id = parsed.group(1), int(parsed.group(2))
    if not await _ensure_match_access(update, await asyncio.to_thread(database.get_match, match_id)):
        return  # _deny_access уже ответил на callback

    try:
        outcome = await asyncio.to_thread(
            database.apply_technical_verdict, match_id, verdict, query.from_user.id
        )
    except Exception as e:
        logger.exception(f"Technical verdict {verdict} failed for match #{match_id}: {e}")
        await query.answer("❌ Не удалось назначить результат.", show_alert=True)
        return

    alert = _VERDICT_ALERTS[verdict]
    if outcome["is_debt"] and not outcome["applied"]:
        alert += "\nВарны по этому долгу уже выданы — изменён только счёт."
    elif not outcome["is_debt"]:
        alert += "\nМатч ещё не долг — варны не начислялись."
    await query.answer(alert, show_alert=True)

    await _notify_group_about_tp(context, match_id, verdict, is_debt=outcome["is_debt"])
    if outcome["applied"]:
        try:
            await _report_technical_verdict(context, match_id, verdict, outcome)
        except Exception as e:
            logger.warning(f"Failed to report technical verdict for match #{match_id}: {e}")
    try:
        from handlers.cabinet import refresh_debts_summary
        await refresh_debts_summary(context)
    except Exception as e:
        logger.warning(f"Failed to refresh debts summary after verdict #{match_id}: {e}")
    await admin_view_match(update, context, match_id=match_id)


@admin_only
async def admin_reset_match_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute match reset via inline callback button click."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    match_id = int(query.data.replace("admin_reset_match_execute_", ""))
    match = await asyncio.to_thread(database.get_match, match_id)

    if match and not await _ensure_match_access(update, match):
        return

    if not match:
        keyboard = [[InlineKeyboardButton("« Назад", callback_data="admin_main_menu")]]
        await query.edit_message_text("❌ Матч не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    await asyncio.to_thread(database.reset_match, match_id)
    
    player_text = (
        f"🔄 <b>Результат вашего матча в Туре {match['round_number']} был сброшен администратором!</b>\n\n"
        f"⚔️ <b>{html.escape(str(match['player1_nickname'] or '—'))}</b> vs "
        f"<b>{html.escape(str(match['player2_nickname'] or '—'))}</b>\n\n"
        f"Вы можете сыграть матч заново и ввести результаты через меню кабинета."
    )
    for p_id in (match["player1_id"], match["player2_id"]):
        if p_id:
            await safe_send_notification(context.bot, p_id, player_text)
            
    # Refresh view
    await admin_view_match(update, context, match_id=match_id)

# --- Conversational Dialogs for Admin ---

@admin_only
async def admin_add_player_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start player creation flow."""
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    if query:
        await query.answer()

    # Check if username is passed as command argument (e.g. /add_player @sp1r1tVSA)
    args = context.args
    username = None
    if args:
        username = args[0].strip().lstrip("@")

    if username:
        if not username or " " in username:
            text = "❌ Неверный юзернейм. Введите корректный Telegram-юзернейм (без пробелов):"
            if query:
                await query.edit_message_text(text)
            else:
                await update.message.reply_text(text)
            return ADMIN_EXPECT_PLAYER_USERNAME
        
        context.user_data["admin_add_player_username"] = username
        return await admin_show_player_divisions(update, context, username)

    # Ask for username
    text = (
        "➕ <b>Добавление игрока</b>\n\n"
        "Введите Telegram-юзернейм игрока (например, <code>@username</code>):\n\n"
        "<i>(Отправьте /cancel для отмены)</i>"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]]
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_EXPECT_PLAYER_USERNAME

@admin_only
async def admin_add_player_username(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture username and show division selection."""
    username = update.message.text.strip().lstrip("@")
    if not username or " " in username:
        await update.message.reply_text(
            "❌ Неверный юзернейм. Введите корректный Telegram-юзернейм (без пробелов):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]])
        )
        return ADMIN_EXPECT_PLAYER_USERNAME
        
    context.user_data["admin_add_player_username"] = username
    return await admin_show_player_divisions(update, context, username)

@admin_only
async def admin_show_player_divisions(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str) -> int:
    """Display active divisions list for selection."""
    divisions = await asyncio.to_thread(database.get_active_divisions)
    if not divisions:
        text = "❌ Нет активных дивизионов в системе."
        keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]]
        markup = InlineKeyboardMarkup(keyboard)
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        elif update.message:
            await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
        return ConversationHandler.END

    keyboard = []
    row = []
    for div in divisions:
        div_name = div.get("name") or f"Дивизион {div['id']}"
        row.append(InlineKeyboardButton(div_name, callback_data=f"admin_add_player_div_{div['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")])
    markup = InlineKeyboardMarkup(keyboard)

    text = f"📁 <b>Выберите дивизион для игрока @{username}:</b>"

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")

    return ADMIN_EXPECT_PLAYER_DIVISION

@admin_only
async def admin_add_player_div_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle callback from division selection button and show clubs for that division."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    division_id_str = query.data.replace("admin_add_player_div_", "")
    try:
        division_id = int(division_id_str)
    except ValueError:
        await query.edit_message_text("❌ Ошибка при выборе дивизиона.")
        return ConversationHandler.END

    context.user_data["admin_add_player_division_id"] = division_id
    username = context.user_data.get("admin_add_player_username")
    if not username:
        await query.answer("❌ Ошибка: не найден юзернейм.", show_alert=True)
        return ConversationHandler.END

    # Get division teams and users in this division
    teams = await asyncio.to_thread(database.get_division_teams, division_id)
    raw_users = await asyncio.to_thread(database.list_users)
    users = [dict(u) if not isinstance(u, dict) else u for u in raw_users]
    club_to_player = _club_owner_labels(users, division_id)

    keyboard = []
    row = []
    for club in teams:
        occupied_by = club_to_player.get(club.lower())
        if occupied_by:
            btn_text = f"🔴 {club} ({occupied_by})"
        else:
            btn_text = f"🟢 {club} (свободен)"

        row.append(InlineKeyboardButton(btn_text, callback_data=f"assign_club_{club}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # Mandatory button: manual club entry
    keyboard.append([InlineKeyboardButton("✍️ Ввести название вручную", callback_data="admin_add_player_manual_club")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")])
    markup = InlineKeyboardMarkup(keyboard)

    text = (
        f"⚽ <b>Выберите клуб для игрока @{username} (Дивизион {division_id}):</b>\n\n"
        f"<i>(Красным отмечены уже занятые клубы — выбор такого клуба переназначит его новому игроку)</i>"
    )
    await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    return ADMIN_EXPECT_PLAYER_CLUB

@admin_only
async def admin_add_player_club_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle callback from club selection button."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    club = query.data.replace("assign_club_", "")
    username = context.user_data.pop("admin_add_player_username", None)
    division_id = context.user_data.pop("admin_add_player_division_id", 1)

    if not username:
        await query.answer("❌ Ошибка: не найден юзернейм. Возможно, вы уже добавили этого игрока.", show_alert=True)
        return ConversationHandler.END

    # Assign new player to the club with division_id
    temp_id, old_username = await asyncio.to_thread(database.assign_player_to_club, username, club, division_id)

    text = (
        f"✅ <b>Игрок успешно добавлен!</b>\n\n"
        f"👤 <b>Telegram:</b> @{username}\n"
        f"🛡️ <b>Клуб:</b> {club}\n"
        f"📁 <b>Дивизион:</b> {division_id}\n"
        f"🆔 <b>Временный ID:</b> <code>{temp_id}</code>\n\n"
        f"Когда @{username} запустит бота (отправит <code>/start</code>), его аккаунт свяжется автоматически."
    )
    if old_username:
        text += f"\n\n<i>⚠️ Примечание: старый участник @{old_username} был автоматически отвязан от клуба {club} и удален.</i>"

    keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="admin_cancel_player_action")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ConversationHandler.END

@admin_only
async def admin_add_player_manual_club_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Transition to manual club name entry state."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()

    username = context.user_data.get("admin_add_player_username")
    division_id = context.user_data.get("admin_add_player_division_id", 1)

    text = (
        f"✍️ <b>Введите название клуба вручную</b> для игрока @{username} (Дивизион {division_id}):\n\n"
        f"<i>(Отправьте /cancel для отмены)</i>"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_EXPECT_MANUAL_CLUB

@admin_only
async def admin_add_player_manual_club_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Capture manually entered club name and assign player."""
    club = update.message.text.strip()
    if not club:
        await update.message.reply_text(
            "❌ Название клуба не может быть пустым. Введите название клуба:",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]])
        )
        return ADMIN_EXPECT_MANUAL_CLUB

    username = context.user_data.pop("admin_add_player_username", None)
    division_id = context.user_data.pop("admin_add_player_division_id", 1)

    if not username:
        await update.message.reply_text("❌ Ошибка: не найден юзернейм игрока. Диалог завершен.")
        return ConversationHandler.END

    temp_id, old_username = await asyncio.to_thread(database.assign_player_to_club, username, club, division_id)

    text = (
        f"✅ <b>Игрок успешно добавлен!</b>\n\n"
        f"👤 <b>Telegram:</b> @{username}\n"
        f"🛡️ <b>Клуб:</b> {club}\n"
        f"📁 <b>Дивизион:</b> {division_id}\n"
        f"🆔 <b>Временный ID:</b> <code>{temp_id}</code>\n\n"
        f"Когда @{username} запустит бота (отправит <code>/start</code>), его аккаунт свяжется автоматически."
    )
    if old_username:
        text += f"\n\n<i>⚠️ Примечание: старый участник @{old_username} был автоматически отвязан от клуба {club} и удален.</i>"

    keyboard = [[InlineKeyboardButton("« Назад в меню", callback_data="admin_cancel_player_action")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ConversationHandler.END

@admin_only
async def admin_import_players_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start players multiline import flow."""
    query = update.callback_query
    user_id = query.from_user.id if query else update.effective_user.id
    if not is_admin(user_id):
        return ConversationHandler.END
    if query:
        await query.answer()
    
    text = (
        "📊 **Импорт списка участников**\n\n"
        "Отправьте список игроков, где каждый участник с новой строки в формате:\n"
        "`@юзернейм - Название Клуба` (через дефис)\n\n"
        "Пример:\n"
        "`@user1 - Real Madrid`\n"
        "`@user2 - Barcelona`"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]]
    if query:
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ADMIN_EXPECT_IMPORT_TEXT

@admin_only
async def admin_import_players_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse list input, pre-register users, and display results."""
    payload = update.message.text
    lines = payload.split("\n")
    
    added = []
    errors = []
    for line in lines:
        line_clean = line.strip()
        if not line_clean:
            continue
        parts = line_clean.split("-", 1)
        if len(parts) != 2:
            parts = line_clean.split(":", 1)
        if len(parts) != 2:
            errors.append(f"Не удалось распарсить строку: <code>{html.escape(line_clean)}</code>")
            continue
            
        part1 = parts[0].strip()
        part2 = parts[1].strip()
        
        if part2.startswith("@") or (not part1.startswith("@") and "@" in part2):
            username = part2.lstrip("@").strip()
            team_name = part1
        else:
            username = part1.lstrip("@").strip()
            team_name = part2
            
        if not username or not team_name:
            errors.append(f"Пустой юзернейм или клуб в строке: <code>{html.escape(line_clean)}</code>")
            continue
            
        try:
            temp_id, old_username = await asyncio.to_thread(database.assign_player_to_club, username, team_name, 1)
            added.append(f"• @{html.escape(username)} — {html.escape(team_name)} (ID: <code>{temp_id}</code>)")
        except Exception as e:
            errors.append(f"Ошибка при добавлении @{html.escape(username)}: {html.escape(str(e))}")
            
    res = []
    if added:
        res.append("✅ <b>Участники успешно импортированы:</b>")
        res.extend(added)
    if errors:
        res.append("\n⚠️ <b>Ошибки при импорте:</b>")
        res.extend(errors)
        
    await update.message.reply_text(
        "\n".join(res),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К списку участников", callback_data="admin_list_players_page_0")]]),
        parse_mode="HTML"
    )
    return ConversationHandler.END

@admin_only
async def admin_edit_club_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start club modification flow."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.answer()
    
    player_id = int(query.data.replace("admin_edit_club_start_", ""))
    player = await asyncio.to_thread(database.get_user, player_id)
    
    if not player:
        await query.edit_message_text("❌ Игрок не найден.")
        return ConversationHandler.END
        
    context.user_data["admin_edit_player_id"] = player_id
    
    text = (
        f"✏️ <b>Изменение клуба</b>\n\n"
        f"Игрок: @{html.escape(str(player['username']))}\n"
        f"Текущий клуб: {html.escape(player['team_name'] or 'нет')}\n\n"
        f"Введите новое название клуба для этого игрока:"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{player_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_EXPECT_NEW_CLUB

@admin_only
async def admin_edit_club_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save new club name in database."""
    new_club = update.message.text.strip()
    player_id = context.user_data.pop("admin_edit_player_id", None)
    
    if not player_id:
        await update.message.reply_text("Произошла ошибка (не найден ID игрока). Сброс.")
        return ConversationHandler.END
        
    success, msg = await asyncio.to_thread(database.set_player_club, str(player_id), new_club)
    await update.message.reply_text(
        f"{'✅' if success else '❌'} {msg}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{player_id}")]])
    )
    return ConversationHandler.END

@admin_only
async def admin_set_score_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Redirect admin to full interactive manual match result entry (score + goal scorers + assists)."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.answer()
    
    match_id = int(query.data.replace("admin_set_score_start_", ""))
    if not await _ensure_match_access(update, await asyncio.to_thread(database.get_match, match_id)):
        return ConversationHandler.END
    context.user_data["is_admin_reporting"] = True
    query.data = f"cb_report_choice_manual_{match_id}"
    await cb_report_choice_manual(update, context)
    return ConversationHandler.END

@admin_only
async def admin_set_score_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Parse score input, save to DB, and notify players."""
    score_text = update.message.text.strip()
    match_id = context.user_data.get("admin_set_match_id")
    
    if not match_id:
        await update.message.reply_text("Произошла ошибка (не найден ID матча). Сброс.")
        return ConversationHandler.END
        
    parts = score_text.split(":")
    if len(parts) != 2:
        parts = score_text.split("-")
    if len(parts) != 2:
        parts = score_text.split(" ")
        
    try:
        s1 = int(parts[0].strip())
        s2 = int(parts[1].strip())
    except (ValueError, IndexError):
        await update.message.reply_text(
            "❌ Неверный формат счета. Введите результат в формате `хозяева:гости` (например, `3:1`):",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_match_{match_id}")]])
        )
        return ADMIN_EXPECT_MATCH_SCORE

    if s1 < 0 or s2 < 0 or s1 > config.MAX_MATCH_GOALS or s2 > config.MAX_MATCH_GOALS:
        await update.message.reply_text(
            f"❌ Некорректный счёт. Максимальное количество голов: {config.MAX_MATCH_GOALS}.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_match_{match_id}")]])
        )
        return ADMIN_EXPECT_MATCH_SCORE

    match = await asyncio.to_thread(database.get_match, match_id)
    if not match:
        await update.message.reply_text("❌ Матч не найден. Сброс.")
        return ConversationHandler.END
        
    admin_id = update.effective_user.id if update.effective_user else None
    await asyncio.to_thread(database.admin_set_match_score, match_id, s1, s2, admin_id)

    # Debt note computed BEFORE rewards are applied
    try:
        from handlers.cabinet import build_debt_footer
        debt_note = await build_debt_footer(match)
    except Exception:
        debt_note = ""

    await update.message.reply_text(
        f"✅ Счет матча #{match_id} изменен: <b>{s1}:{s2}</b>!",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К карточке матча", callback_data=f"admin_view_match_{match_id}")]])
    )

    # Notify players
    player_text = (
        f"⚙️ <b>Администратор вручную установил результат вашего матча (Тур {match['round_number']})!</b>\n\n"
        f"⚔️ <b>{html.escape(str(match['player1_nickname'] or '—'))}</b>  <code>{s1} : {s2}</code>  "
        f"<b>{html.escape(str(match['player2_nickname'] or '—'))}</b>\n\n"
        f"Результат подтвержден и обновлен в таблице."
    ) + debt_note
    for p_id in (match["player1_id"], match["player2_id"]):
        await safe_send_notification(context.bot, p_id, player_text)

    # Notify Telegram Group (scoped to division topic)
    group_id, target_topic = await resolve_division_target(
        match.get("division_id"), "results", "reports",
        legacy_topic_keys=("results_topic_id",),
    )
    if group_id:
        group_text = (
            f"⚙️ **Результат матча изменен администратором!**\n"
            f"🏆 **Тур {match['round_number']}**\n"
            f"⚔️ **{match['player1_nickname']}** ({match['player1_team'] or 'нет'}) "
            f"**{s1} : {s2}** "
            f"**{match['player2_nickname']}** ({match['player2_team'] or 'нет'})"
        ) + debt_note.replace("<b>", "**").replace("</b>", "**")

        kwargs = {"chat_id": group_id, "text": group_text, "parse_mode": "Markdown"}
        if target_topic:
            kwargs["message_thread_id"] = int(target_topic)
        try:
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logger.exception("Не удалось отправить сообщение в группу")

    try:
        await post_league_table_to_reports(context)
    except Exception:
        logger.exception("Failed to refresh league table after manual score")

    # Process debt reward (-1 warn) and all-debts-cleared notification
    try:
        from handlers.cabinet import handle_debt_played_rewards, refresh_debts_summary
        await handle_debt_played_rewards(
            context,
            match_id=match_id,
            round_number=match['round_number'],
            p1_id=match.get('player1_id'),
            p2_id=match.get('player2_id')
        )
        await refresh_debts_summary(context)
    except Exception as e:
        logger.warning(f"Failed to process debt reward for admin score set: {e}")

    return ConversationHandler.END

@admin_only
async def admin_cancel_player_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort player edits and return to players hub or admin panel."""
    query = update.callback_query
    context.user_data.pop("admin_add_player_username", None)
    context.user_data.pop("admin_add_player_division_id", None)
    context.user_data.pop("admin_edit_player_id", None)
    
    if query:
        await query.answer()
        await admin_manage_players_info(update, context)
    else:
        await show_admin_panel(update, context)
    return ConversationHandler.END

@admin_only
async def admin_cancel_match_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Abort match edits and return to match card."""
    query = update.callback_query
    match_id = context.user_data.pop("admin_set_match_id", None)
    # Отмена общая для карточки матча и для FSM открытия туров — чистим оба.
    round_number = context.user_data.pop("admin_round_to_open", None)
    div_id = context.user_data.pop("admin_round_open_div", None)
    context.user_data.pop("batch_div_id", None)
    context.user_data.pop("batch_start", None)
    context.user_data.pop("batch_end", None)

    if query:
        await query.answer()
        if match_id:
            await admin_view_match(update, context, match_id=match_id)
        elif div_id and round_number:
            await _render_div_round_card(query, context, div_id, round_number)
        else:
            await show_admin_panel(update, context)
    else:
        await show_admin_panel(update, context)
    return ConversationHandler.END

@admin_only
async def admin_toggle_role(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle user system role between player and admin."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    # Format: admin_toggle_role_{player_id}_{new_role}
    parts = query.data.split("_")
    player_id = int(parts[3])
    new_role = parts[4]
    
    # Safety: do not allow admins to revoke their own admin rights
    if player_id == query.from_user.id and new_role == "player":
        await query.message.reply_text("❌ Вы не можете снять роль администратора с себя.")
        return
        
    success, msg = await asyncio.to_thread(database.update_player_role, player_id, new_role)
    if success:
        # Refresh player card
        await admin_view_player(update, context, player_id=player_id)
    else:
        await query.message.reply_text(f"❌ Ошибка: {msg}")

@admin_only
async def admin_delete_options(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show options screen for player deletion (soft exclusion vs complete wipe)."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    player_id = int(query.data.replace("admin_delete_options_", ""))
    player = await asyncio.to_thread(database.get_user, player_id)
    
    if not player:
        keyboard = [[InlineKeyboardButton("« Назад к списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
        
    text = (
        f"❌ <b>Удаление участника @{html.escape(str(player['username']))}</b>\n\n"
        f"Выберите тип удаления:\n\n"
        f"1. <b>Исключить (матчи сохранить)</b>:\n"
        f"Сыгранные матчи остаются в истории лиги, несыгранные — в расписании и ждут нового владельца клуба.\n\n"
        f"2. <b>Стереть полностью (Без следов)</b>:\n"
        f"Полностью удаляет игрока и <b>все матчи с его участием</b> (включая уже сыгранные)."
    )

    keyboard = [
        [InlineKeyboardButton("🗑️ 1. Исключить (матчи сохранить)", callback_data=f"admin_confirm_delete_player_{player_id}")],
        [InlineKeyboardButton("🔥 2. Стереть полностью (Без следов)", callback_data=f"admin_confirm_wipe_player_{player_id}")],
        [InlineKeyboardButton("« Назад к карточке", callback_data=f"admin_view_player_{player_id}")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

@admin_only
async def admin_confirm_wipe_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show final warning for complete player wipe."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    player_id = int(query.data.replace("admin_confirm_wipe_player_", ""))
    player = await asyncio.to_thread(database.get_user, player_id)
    
    if not player:
        keyboard = [[InlineKeyboardButton("« Назад к списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return
        
    text = (
        f"⚠️ <b>ВНИМАНИЕ: ПОЛНОЕ УДАЛЕНИЕ</b>\n\n"
        f"Вы действительно хотите безвозвратно стереть игрока @{html.escape(str(player['username']))} "
        f"и ВСЕ матчи с его участием?\n\n"
        f"<b>Это действие удалит сыгранные им матчи и изменит турнирные расклады остальных участников!</b>"
    )
    
    keyboard = [
        [InlineKeyboardButton("🔥 Да, стереть полностью", callback_data=f"admin_wipe_player_execute_{player_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{player_id}")]
    ]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

@admin_only
async def admin_wipe_player_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute complete player wipe from database."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()
    
    player_id = int(query.data.replace("admin_wipe_player_execute_", ""))
    success, msg = await asyncio.to_thread(database.delete_player_completely, player_id)
    
    keyboard = [[InlineKeyboardButton("« Назад к списку", callback_data="admin_list_players_page_0")]]
    if success:
        await query.edit_message_text(f"✅ {html.escape(msg)}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
        
        group_id = await asyncio.to_thread(database.get_group_id)
        if group_id:
            try:
                await context.bot.send_message(chat_id=group_id, text=f"📢 <b>Полное удаление участника!</b>\n\n{html.escape(msg)}", parse_mode="HTML")
            except Exception as e:
                logger.exception("Не удалось отправить уведомление в группу")
    else:
        await query.edit_message_text(f"❌ {msg}", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Conversation handlers for username / nickname / reset ---



@admin_only
async def admin_edit_username_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start Telegram username edit flow."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return ConversationHandler.END
    await query.answer()
    
    player_id = int(query.data.replace("admin_edit_username_start_", ""))
    player = await asyncio.to_thread(database.get_user, player_id)
    
    if not player:
        await query.edit_message_text("❌ Игрок не найден.")
        return ConversationHandler.END
        
    context.user_data["admin_edit_player_id"] = player_id
    
    text = (
        f"✏️ <b>Изменение юзернейма</b>\n\n"
        f"Игрок: @{html.escape(str(player['username']))}\n"
        f"Текущий юзернейм: @{html.escape(player['username'] or 'нет')}\n\n"
        f"Введите новый Telegram-юзернейм (например, <code>@username</code>):"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{player_id}")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")
    return ADMIN_EXPECT_NEW_USERNAME

@admin_only
async def admin_edit_username_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Save new Telegram username in database."""
    new_username = update.message.text.strip().lstrip("@")
    player_id = context.user_data.pop("admin_edit_player_id", None)
    
    if not player_id:
        await update.message.reply_text("Произошла ошибка (не найден ID игрока). Сброс.")
        return ConversationHandler.END
        
    success, msg = await asyncio.to_thread(database.update_player_username, player_id, new_username)
    await update.message.reply_text(
        f"✅ {msg}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{player_id}")]])
    )
    return ConversationHandler.END

# --- Reset League Flow ---

@admin_only
async def admin_clear_league_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start full league reset flow."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return ConversationHandler.END

    import config
    if query.from_user.id not in config.ADMIN_IDS:
        await query.answer("❌ Сброс всей лиги разрешён только Главному Администратору!", show_alert=True)
        return ConversationHandler.END

    await query.answer()
    
    text = (
        "⚠️ **СБРОС ВСЕЙ ЛИГИ**\n\n"
        "Внимание! Это действие удалит всех зарегистрированных участников и все сгенерированные матчи.\n"
        "Все пользователи с ролью Admin будут сохранены.\n\n"
        "Для подтверждения сброса, пожалуйста, отправьте кодовое слово **СБРОС** большими буквами:\n"
        "*(Или нажмите Отмена ниже)*"
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data="admin_cancel_player_action")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ADMIN_EXPECT_RESET_CONFIRM

@admin_only
async def admin_clear_league_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Confirm text word and wipe the database tables."""
    import config
    if not update.effective_user or update.effective_user.id not in config.ADMIN_IDS:
        await update.message.reply_text("❌ Сброс всей лиги разрешён только Главному Администратору!")
        return ConversationHandler.END

    text_input = update.message.text.strip()
    
    if text_input != "СБРОС":
        await update.message.reply_text(
            "❌ Кодовое слово введено неверно. Сброс лиги отменен.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Управление участниками", callback_data="admin_manage_players_info")]])
        )
        return ConversationHandler.END
        
    await asyncio.to_thread(database.clear_entire_league)
    try:
        await asyncio.to_thread(
            database.log_admin_action,
            admin_id=update.effective_user.id,
            action="clear_entire_league",
            target_type="system",
            reason="Confirmed by keyword СБРОС"
        )
    except Exception as e:
        logger.warning(f"Failed to log clear_entire_league action: {e}")
    
    await update.message.reply_text(
        "✅ **Все матчи и игроки успешно удалены!**\n\nБаза данных очищена (за исключением администраторов). Вы можете добавлять новый список участников и генерировать расписание заново.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Управление участниками", callback_data="admin_manage_players_info")]]),
        parse_mode="Markdown"
    )
    
    # Notify group if configured
    group_id = await asyncio.to_thread(database.get_group_id)
    if group_id:
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text="📢 **Лига сброшена администратором!**\n\nВсе игроки и расписание матчей были очищены.",
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.exception("Не удалось отправить уведомление в группу")
            
    return ConversationHandler.END


@admin_only
async def admin_remove_player_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command to remove a player by @username."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        return
        
    args = context.args
    if not args:
        await update.message.reply_text("❌ Использование: <code>/remove_player @username</code>", parse_mode="HTML")
        return
        
    target = args[0].strip()
    # Разрешаем ссылку в игрока до удаления — потом строки в users уже не будет.
    player = await asyncio.to_thread(database.find_user_by_ref, target)

    success, msg = await asyncio.to_thread(database.remove_player, target)
    if success:
        await update.message.reply_text(f"✅ {html.escape(msg)}", parse_mode="HTML")
        if player:
            username_str = f"@{player['username']}" if player.get("username") else f"ID: {player['telegram_id']}"
            await _announce_player_exclusion(
                context,
                player.get("division_id"),
                int(player["telegram_id"]),
                username_str,
                player.get("team_name") or "без названия"
            )
    else:
        await update.message.reply_text(f"❌ Ошибка: {msg}")


@admin_only
async def admin_list_players_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command to list all 16 clubs and who is assigned to them."""
    user = update.effective_user
    if not user:
        return
        
    # Get active mapping of club to player row
    raw_users = await asyncio.to_thread(database.list_users)
    users = [dict(u) if not isinstance(u, dict) else u for u in raw_users]
    club_to_player = {u["team_name"].lower(): u for u in users if u.get("team_name")}

    divisions = await asyncio.to_thread(database.get_active_divisions)
    all_clubs = set()
    for div in divisions:
        teams = await asyncio.to_thread(database.get_division_teams, div["id"])
        all_clubs.update(teams)
    for u in users:
        if u.get("team_name"):
            all_clubs.add(u["team_name"])

    lines = ["📋 <b>Текущий состав участников и клубов:</b>\n"]
    for club in sorted(all_clubs):
        user_row = club_to_player.get(club.lower())
        if user_row:
            status = "✅" if user_row["telegram_id"] > 0 else "⏳ ждёт старта"
            lines.append(f"🔴 <b>{club}</b> — @{user_row['username']} ({status})")
        else:
            lines.append(f"🟢 <b>{club}</b> — <i>свободен</i>")
            
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


# --- Conversation state for squad upload ---
ADMIN_EXPECT_SQUAD_TEXT = 201
ADMIN_EXPECT_SINGLE_PLAYER = 202


@admin_only
async def admin_manage_players_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the interactive player management menu."""
    query = update.callback_query
    if query:
        await query.answer()
    
    user_id = query.from_user.id if query else update.effective_user.id
    if not is_admin(user_id):
        if query: await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    users = await asyncio.to_thread(database.list_users)
    total_count = len(users)

    text = (
        f"👥 <b>Управление игроками лиги</b>\n\n"
        f"Зарегистрировано участников: <b>{total_count}</b>\n\n"
        f"Выберите действие в меню ниже:"
    )

    keyboard = [
        [InlineKeyboardButton("📋 Список участников", callback_data="admin_list_players_page_0")],
        [InlineKeyboardButton("➕ Добавить игрока", callback_data="admin_add_player_start")],
        [InlineKeyboardButton("📥 Массовый импорт (списком)", callback_data="admin_import_players_start")],
        [InlineKeyboardButton("🔄 Сбросить варны (новый сезон)", callback_data="admin_reset_season_warns")],
        [InlineKeyboardButton("🗑 Очистить всю лигу", callback_data="admin_clear_league_start")],
        [InlineKeyboardButton("« Назад в админ-панель", callback_data="admin_main_menu")]
    ]
    markup = InlineKeyboardMarkup(keyboard)

    if query:
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

@admin_only
async def admin_list_players_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int | None = None) -> None:
    """Paginated list of players with inline buttons for each player."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    if page is None:
        page = 0
        if query.data and query.data.startswith("admin_list_players_page_"):
            try:
                page = int(query.data.replace("admin_list_players_page_", ""))
            except ValueError:
                page = 0

    users = await asyncio.to_thread(database.list_users)
    if not users:
        keyboard = [
            [InlineKeyboardButton("➕ Добавить игрока", callback_data="admin_add_player_start")],
            [InlineKeyboardButton("« Назад", callback_data="admin_manage_players_info")]
        ]
        await query.edit_message_text("👥 <b>Участники не найдены.</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    PER_PAGE = 6
    total_pages = (len(users) + PER_PAGE - 1) // PER_PAGE
    if page < 0: page = 0
    if page >= total_pages: page = total_pages - 1

    start_idx = page * PER_PAGE
    page_users = users[start_idx : start_idx + PER_PAGE]

    keyboard = []
    for u in page_users:
        u_name = f"@{u['username']}" if u['username'] else f"ID: {u['telegram_id']}"
        team_name = u['team_name'] or 'Без клуба'
        btn_text = f"👤 {u_name} — {team_name}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_view_player_{u['telegram_id']}")])

    # Pagination row
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_list_players_page_{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"admin_list_players_page_{page + 1}"))
    keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("➕ Добавить игрока", callback_data="admin_add_player_start")])
    keyboard.append([InlineKeyboardButton("« Назад в меню", callback_data="admin_manage_players_info")])

    text = f"📋 <b>Список участников лиги (Стр. {page + 1}/{total_pages}):</b>\n\nВыберите игрока для управления:"
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_view_player(update: Update, context: ContextTypes.DEFAULT_TYPE, player_id: int | None = None) -> None:
    """View detailed player card with inline action buttons."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    if player_id is None:
        p_id = int(query.data.replace("admin_view_player_", ""))
    else:
        p_id = player_id
    player = await asyncio.to_thread(database.get_user, p_id)

    if not player:
        keyboard = [[InlineKeyboardButton("« К списку участников", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    username_str = f"@{player['username']}" if player['username'] else "(без юзернейма)"
    team_str = player['team_name'] or 'Без клуба'
    role_str = "Администратор" if player['role'] == 'admin' else "Игрок"
    warn_count = player.get('warn_count', 0) if isinstance(player, dict) else player['warn_count']

    div_name = "Не назначен"
    p_dict = dict(player) if player else {}
    if p_dict.get('division_id'):
        div_row = await asyncio.to_thread(database.get_division, p_dict['division_id'])
        if div_row:
            div_name = div_row['name']

    text = (
        f"👤 <b>Карточка участника:</b>\n\n"
        f"• <b>Telegram:</b> {html.escape(username_str)}\n"
        f"• <b>Клуб:</b> {html.escape(team_str)}\n"
        f"• <b>Дивизион:</b> {html.escape(div_name)}\n"
        f"• <b>Telegram ID:</b> <code>{player['telegram_id']}</code>\n"
        f"• <b>Роль:</b> {role_str}\n"
        f"• <b>Варны:</b> {warn_count} / {MAX_WARNS_LIMIT}\n"
    )

    keyboard = [
        [
            InlineKeyboardButton("✏️ Изменить клуб", callback_data=f"admin_edit_club_select_{p_id}"),
            InlineKeyboardButton("🏆 Дивизион", callback_data=f"admin_edit_div_select_{p_id}")
        ],
        [InlineKeyboardButton("✏️ Изменить юзернейм", callback_data=f"admin_edit_username_start_{p_id}")],
        [
            InlineKeyboardButton("➕ Выдать варн", callback_data=f"warn_add_{p_id}"),
            InlineKeyboardButton("➖ Снять варн", callback_data=f"warn_remove_{p_id}")
        ],
        [
            InlineKeyboardButton("📜 История варнов", callback_data=f"warn_hist_{p_id}"),
            InlineKeyboardButton("🕊 Амнистия", callback_data=f"warn_amnesty_{p_id}")
        ],
        [InlineKeyboardButton("🗑 Исключить из лиги", callback_data=f"admin_delete_player_confirm_{p_id}")],
    ]
    # Админ возвращается туда, откуда пришёл (в список дивизиона или общий список).
    p_div_id = p_dict.get("division_id")
    back_cb = context.user_data.get("admin_player_back_cb") if context and context.user_data else None
    if back_cb:
        if "div_players" in back_cb:
            back_text = "« К участникам дивизиона"
        else:
            back_text = "« К списку участников"
        keyboard.append([InlineKeyboardButton(back_text, callback_data=back_cb)])
    elif p_div_id and not is_global_admin(query.from_user.id):
        keyboard.append([InlineKeyboardButton("« К участникам дивизиона", callback_data=f"admin_div_players:{p_div_id}:0")])
    else:
        keyboard.append([InlineKeyboardButton("« К списку участников", callback_data="admin_list_players_page_0")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

def _club_owner_labels(users: list[dict], division_id: int | None = None) -> dict[str, str]:
    """Клуб (lowercase) → как обратиться к его нынешнему владельцу.

    Тренер без @username всё равно владеет клубом, поэтому подписываемся его ID:
    ключ по одному `username` выбрасывал бы такого из карты, клуб рисовался бы
    «свободен», и админ переназначил бы его, не зная, что отбирает — set_player_club
    снимает прежнего владельца молча.
    """
    labels: dict[str, str] = {}
    for u in users:
        club = (u.get("team_name") or "").strip()
        if not club:
            continue
        if division_id is not None and u.get("division_id") != division_id:
            continue
        labels[club.lower()] = f"@{u['username']}" if u.get("username") else f"ID {u.get('telegram_id')}"
    return labels


async def _club_choices_for_player(player) -> list[str]:
    """Клубы, предлагаемые одному игроку, в устойчивом порядке.

    Telegram возвращает из кнопки только индекс, поэтому `admin_edit_club_execute`
    вынуждена собрать ровно тот же список, который пронумеровала `admin_edit_club_select`
    (после перезапуска бота `user_data` пуст). Отсюда один общий помощник и
    `sorted` на запасном пути: `get_all_teams` порядок не гарантирует, а сдвиг на
    одну позицию привязал бы тренера к соседнему клубу.
    """
    row = dict(player) if player is not None else {}
    div_id = row.get("division_id") or 1
    choices = await asyncio.to_thread(database.get_division_teams, div_id)
    if not choices:
        choices = sorted(await asyncio.to_thread(database.get_all_teams))
    return choices


@admin_only
async def admin_edit_club_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show grid of inline buttons for clubs in the division to edit player's club."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    p_id = int(query.data.replace("admin_edit_club_select_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)

    if not player:
        keyboard = [[InlineKeyboardButton("« К списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    raw_users = await asyncio.to_thread(database.list_users)
    users = [dict(u) if not isinstance(u, dict) else u for u in raw_users]
    club_to_player = _club_owner_labels(users)

    division_teams = await _club_choices_for_player(player)

    context.user_data[f"admin_edit_clubs_{p_id}"] = division_teams

    keyboard = []
    row = []

    for club_idx, club in enumerate(division_teams):
        occupied_by = club_to_player.get(club.lower())
        if player['team_name'] and player['team_name'].lower() == club.lower():
            btn_text = f"⭐ {club} (текущий)"
        elif occupied_by:
            btn_text = f"🔴 {club} ({occupied_by})"
        else:
            btn_text = f"🟢 {club} (свободен)"

        # Use club index instead of full club name to stay under Telegram's 64-byte callback_data limit
        row.append(InlineKeyboardButton(btn_text, callback_data=f"admin_eclub_{p_id}_{club_idx}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{p_id}")])

    text = (
        f"⚽ <b>Выберите новый клуб для игрока {html.escape('@' + player['username'] if player['username'] else str(p_id))}:</b>\n\n"
        f"<i>(Клик по кнопке моментально сменит клуб)</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_edit_club_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute club change via inline button click."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    # Parse: admin_eclub_{p_id}_{club_idx}
    data_parts = query.data.replace("admin_eclub_", "").split("_", 1)
    if len(data_parts) != 2:
        await query.answer()
        return
    p_id = int(data_parts[0])
    club_idx = int(data_parts[1])

    clubs_list = context.user_data.get(f"admin_edit_clubs_{p_id}")
    if not clubs_list:
        player = await asyncio.to_thread(database.get_user, p_id)
        clubs_list = await _club_choices_for_player(player)

    if not clubs_list or club_idx < 0 or club_idx >= len(clubs_list):
        await query.answer("❌ Неверный индекс клуба.", show_alert=True)
        return
    new_club = clubs_list[club_idx]

    success, msg = await asyncio.to_thread(database.set_player_club, str(p_id), new_club)
    if success:
        try:
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to update debts in warns on club edit: {e}")
    # Use single query.answer() with the result message to avoid BadRequest: query already answered
    await query.answer(f"✅ {msg}" if success else f"❌ {msg}", show_alert=True)

    await admin_view_player(update, context, player_id=p_id)


# ─── Привязка клубов: взгляд «от клуба», а не «от игрока» ────────────────────
#
# «Изменить клуб» в карточке игрока отвечает на вопрос «какой клуб дать этому
# тренеру». Здесь обратный вопрос — «кто сидит в этих 16 клубах и какие ещё
# свободны». Клуб едет в callback_data индексом в `get_division_teams`: список
# отсортирован и детерминирован, а названия кириллицей (2 байта на символ) в
# 64-байтный лимит Telegram не влезают. `user_data` в цепочке не участвует
# вообще — кнопка со вчерашнего сообщения работает и после перезапуска бота.


_BIND_FROM_HUB = "h"


def _bind_parse(data: str) -> list[int]:
    """Числовые части callback_data привязки: `admin_bind_*:{a}:{b}:…` → [a, b, …].

    Хвостовую метку происхождения (`:h`) пропускаем — она не число.
    """
    return [int(part) for part in data.split(":")[1:] if part.lstrip("-").isdigit()]


def _bind_origin(data: str) -> str:
    """Метка входа в экран привязки: `:h` для пути из хаба, иначе пусто.

    Экран клубов открывается из трёх мест — хаба привязки, карточки дивизиона
    и панели админа дивизиона, — и «Назад» обязан вести туда, откуда пришли.
    Роль для этого не годится: супер-админ попадает сюда обоими путями, и его
    из хаба выбрасывало в карточку дивизиона. Метку тащим через callback_data,
    а не через user_data: кнопка в старом сообщении обязана работать и после
    перезапуска бота.
    """
    return f":{_BIND_FROM_HUB}" if data.endswith(f":{_BIND_FROM_HUB}") else ""


def _bind_back_cb(update: Update, div_id: int, origin: str) -> str:
    """Куда ведёт «Назад» с экрана клубов дивизиона."""
    return "admin_bind_hub" if origin else _div_home_cb(update, div_id)


async def _bind_all_users() -> list[dict]:
    """Вся лига словарями: `list_users` отдаёт `sqlite3.Row`, у которых нет `.get()`."""
    return [dict(u) for u in await asyncio.to_thread(database.list_users)]


def _bind_club_owner(users: list[dict], club: str) -> dict | None:
    """Нынешний владелец клуба или None.

    Сверка точная по lower/strip — ровно как в `_club_owner_labels`, иначе сетка
    клубов и карточка клуба разошлись бы во мнении, занят ли клуб. Ищем по всей
    лиге, а не по дивизиону: тренер мог остаться приписанным к чужому дивизиону,
    и фильтр нарисовал бы занятый клуб свободным.
    """
    needle = club.strip().lower()
    for u in users:
        if (u.get("team_name") or "").strip().lower() == needle:
            return u
    return None


async def _bind_candidates(div_id: int, exclude_id: int | None = None) -> list[dict]:
    """Кого можно посадить в клуб дивизиона.

    Сначала участники дивизиона без клуба — ради них экран и существует, затем
    занятые (это и есть «замена»), в конце тренеры вообще без дивизиона:
    `admin_bind_execute` проставит им `division_id` при привязке.
    """
    in_div = await asyncio.to_thread(database.get_division_users, div_id)
    no_div = await asyncio.to_thread(database.get_division_users, None)
    free = [u for u in in_div if not (u.get("team_name") or "").strip()]
    busy = [u for u in in_div if (u.get("team_name") or "").strip()]
    ordered = free + busy + no_div
    return [u for u in ordered if u["telegram_id"] != exclude_id]


def _bind_candidate_label(candidate: dict, div_id: int) -> str:
    """Подпись кнопки участника: свободен / с чьим клубом / не из дивизиона."""
    name = f"@{candidate['username']}" if candidate.get("username") else f"ID {candidate['telegram_id']}"
    club = (candidate.get("team_name") or "").strip()
    if candidate.get("division_id") != div_id:
        return f"🆕 {name} (без дивизиона){f' — {club}' if club else ''}"
    if club:
        return f"🔁 {name} — {club}"
    return f"🆓 {name}"


async def _bind_render_division(
    update: Update, context: ContextTypes.DEFAULT_TYPE, div_id: int, origin: str = ""
) -> None:
    """Экран клубов дивизиона со статусами занятости."""
    query = update.callback_query
    division = await asyncio.to_thread(database.get_division, div_id)
    div_name = division["name"] if division else f"#{div_id}"
    teams = await asyncio.to_thread(database.get_division_teams, div_id)

    back_row = [InlineKeyboardButton("« Назад", callback_data=_bind_back_cb(update, div_id, origin))]
    if not teams:
        # Немое «(0/0)» выглядело как «клубы ещё не завели». На деле состав
        # ищется по коду дивизиона, и пустой экран значит, что код не совпал
        # с ключом сезонного ростера — это видно только если код показать.
        div_code = (division or {}).get("code") or "—"
        await query.edit_message_text(
            f"⚠️ К дивизиону <b>{html.escape(str(div_name))}</b> не привязан состав клубов.\n\n"
            f"Клубы сезона берутся по коду дивизиона, а код <code>{html.escape(str(div_code))}</code> "
            f"в ростере не значится — поэтому привязывать нечего.\n\n"
            f"Клубы появятся, когда код совпадёт с ключом ростера "
            f"(<code>DIV_1</code>…<code>DIV_5</code>) или когда в дивизионе "
            f"зарегистрируется первый участник со своим клубом.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([back_row])
        )
        return

    owners = _club_owner_labels(await _bind_all_users())

    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    taken = 0
    for club_idx, club in enumerate(teams):
        owner = owners.get(club.lower())
        if owner:
            taken += 1
            btn_text = f"🔴 {club} ({owner})"
        else:
            btn_text = f"🟢 {club} (свободен)"
        row.append(InlineKeyboardButton(btn_text, callback_data=f"admin_bind_club:{div_id}:{club_idx}:0{origin}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append(back_row)

    text = (
        f"🔗 <b>Привязка клубов — {html.escape(str(div_name))}</b>\n\n"
        f"Занято: <b>{taken}/{len(teams)}</b>\n\n"
        f"Выберите клуб, чтобы назначить или сменить его владельца:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_bind_hub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Супер-админ: выбор дивизиона для привязки клубов."""
    query = update.callback_query
    if not query or not await _ensure_super_admin(update):
        return

    owners = _club_owner_labels(await _bind_all_users())

    divisions = await asyncio.to_thread(database.get_divisions)
    keyboard = []
    for d in divisions:
        teams = await asyncio.to_thread(database.get_division_teams, d["id"])
        taken = sum(1 for t in teams if t.lower() in owners)
        status_icon = "🟢" if d.get("is_active") else "🔴"
        # Дивизион без клубов — не «пока никто не занял», а сломанная привязка
        # ростера; счётчик «0/0» это скрывал.
        counter = f"({taken}/{len(teams)})" if teams else "⚠️ нет клубов"
        label = f"{status_icon} {d['name']} {counter}"
        # `:h` — метка входа из хаба, чтобы «Назад» вернул сюда же.
        keyboard.append([InlineKeyboardButton(
            label, callback_data=f"admin_bind_div:{d['id']}:{_BIND_FROM_HUB}"
        )])
    keyboard.append([InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")])

    text = (
        "🔗 <b>Привязка клубов</b>\n\n"
        "Клубы закреплены за дивизионом, участники — за клубами.\n"
        "В скобках — сколько клубов дивизиона уже разобрано.\n\n"
        "Выберите дивизион:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_bind_division(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Клубы дивизиона со статусами занятости."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    div_id = _bind_parse(query.data)[0]
    if not await _ensure_division_access(update, div_id):
        return

    await _bind_render_division(update, context, div_id, _bind_origin(query.data))


@admin_only
async def admin_bind_club_card(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Карточка клуба: владелец и список кандидатов на привязку."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    div_id, club_idx, page = _bind_parse(query.data)
    origin = _bind_origin(query.data)
    if not await _ensure_division_access(update, div_id):
        return

    teams = await asyncio.to_thread(database.get_division_teams, div_id)
    if club_idx < 0 or club_idx >= len(teams):
        await _bind_render_division(update, context, div_id, origin)
        return
    club = teams[club_idx]

    owner = _bind_club_owner(await _bind_all_users(), club)
    owner_id = owner["telegram_id"] if owner else None
    owner_label = None
    if owner:
        owner_label = f"@{owner['username']}" if owner.get("username") else f"ID {owner['telegram_id']}"

    candidates = await _bind_candidates(div_id, exclude_id=owner_id)

    per_page = 8
    total_pages = max(1, (len(candidates) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    page_candidates = candidates[page * per_page : page * per_page + per_page]

    keyboard = []
    for c in page_candidates:
        label = _bind_candidate_label(c, div_id)
        keyboard.append([InlineKeyboardButton(label, callback_data=f"admin_bind_set:{div_id}:{club_idx}:{c['telegram_id']}{origin}")])

    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"admin_bind_club:{div_id}:{club_idx}:{page - 1}{origin}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1} / {total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"admin_bind_club:{div_id}:{club_idx}:{page + 1}{origin}"))
        keyboard.append(nav_row)

    if owner_id is not None:
        keyboard.append([InlineKeyboardButton(
            "🗑 Освободить клуб", callback_data=f"admin_bind_free:{div_id}:{club_idx}{origin}"
        )])
    keyboard.append([InlineKeyboardButton("« К клубам", callback_data=f"admin_bind_div:{div_id}{origin}")])

    lines = [f"⚽ <b>{html.escape(club)}</b>\n"]
    if owner_label:
        lines.append(f"Сейчас клубом владеет <b>{html.escape(owner_label)}</b>.")
        lines.append("Привязка другого участника отберёт клуб и обнулит варны прежнего владельца.\n")
    else:
        lines.append("Клуб <b>свободен</b>.\n")
    if page_candidates:
        lines.append("Выберите участника — клик сразу привяжет его к клубу:")
    else:
        lines.append("<i>Нет участников, которых можно привязать.</i>")

    await query.edit_message_text(
        "\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def admin_bind_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Привязать выбранного участника к клубу.

    Без @admin_only намеренно: декоратор гасит callback пустым query.answer(),
    а Telegram принимает ответ на запрос только один раз — итоговый алерт с
    результатом привязки тогда не долетает. Права проверяются вручную.
    """
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _deny_access(update)
        return

    div_id, club_idx, player_id = _bind_parse(query.data)
    origin = _bind_origin(query.data)
    if not await _ensure_division_access(update, div_id):
        return

    teams = await asyncio.to_thread(database.get_division_teams, div_id)
    if club_idx < 0 or club_idx >= len(teams):
        await query.answer("❌ Клуб не найден.", show_alert=True)
        return
    club = teams[club_idx]

    player = await asyncio.to_thread(database.get_user, player_id)
    player_row = dict(player) if player else {}

    success, msg = await asyncio.to_thread(database.set_player_club, str(player_id), club)
    if success:
        # Клуб принадлежит дивизиону, поэтому его владелец обязан в нём числиться:
        # иначе тренер выпадет из таблицы, долгов и дайджестов — они считаются
        # по division_id, а не по названию клуба.
        if player_row.get("division_id") != div_id:
            await asyncio.to_thread(database.assign_user_division, player_id, div_id)
            msg += " Участник переведён в этот дивизион."
        try:
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to update debts in warns on club bind: {e}")

    await query.answer(f"✅ {msg}" if success else f"❌ {msg}", show_alert=True)
    await _bind_render_division(update, context, div_id, origin)


@admin_only
async def admin_bind_free_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подтверждение освобождения клуба."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    div_id, club_idx = _bind_parse(query.data)
    origin = _bind_origin(query.data)
    if not await _ensure_division_access(update, div_id):
        return

    teams = await asyncio.to_thread(database.get_division_teams, div_id)
    if club_idx < 0 or club_idx >= len(teams):
        await _bind_render_division(update, context, div_id, origin)
        return
    club = teams[club_idx]

    owner = _bind_club_owner(await _bind_all_users(), club)
    if not owner:
        await _bind_render_division(update, context, div_id, origin)
        return
    owner_label = f"@{owner['username']}" if owner.get("username") else f"ID {owner['telegram_id']}"

    text = (
        f"🗑 <b>Освободить клуб {html.escape(club)}?</b>\n\n"
        f"Владелец <b>{html.escape(owner_label)}</b> останется в лиге и в дивизионе, "
        f"но без клуба. Варны и их история будут сброшены.\n\n"
        f"Матчи клуба останутся за клубом — их унаследует следующий владелец."
    )
    keyboard = [
        [InlineKeyboardButton("✅ Да, освободить", callback_data=f"admin_bind_free_ok:{div_id}:{club_idx}{origin}")],
        [InlineKeyboardButton("« Отмена", callback_data=f"admin_bind_club:{div_id}:{club_idx}:0{origin}")],
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_bind_free_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снять клуб с его нынешнего владельца. Без @admin_only — см. admin_bind_execute."""
    query = update.callback_query
    if not query:
        return
    user = update.effective_user
    if not user or not is_admin(user.id):
        await _deny_access(update)
        return

    div_id, club_idx = _bind_parse(query.data)
    origin = _bind_origin(query.data)
    if not await _ensure_division_access(update, div_id):
        return

    teams = await asyncio.to_thread(database.get_division_teams, div_id)
    if club_idx < 0 or club_idx >= len(teams):
        await query.answer("❌ Клуб не найден.", show_alert=True)
        return
    club = teams[club_idx]

    owner = _bind_club_owner(await _bind_all_users(), club)
    if not owner:
        await query.answer("❌ Клуб и так свободен.", show_alert=True)
        await _bind_render_division(update, context, div_id, origin)
        return

    success, msg = await asyncio.to_thread(database.clear_player_club, int(owner["telegram_id"]))
    if success:
        try:
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to update debts in warns on club release: {e}")

    await query.answer(f"✅ {msg}" if success else f"❌ {msg}", show_alert=True)
    await _bind_render_division(update, context, div_id, origin)


@admin_only
async def admin_edit_div_select(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of active divisions to assign player to."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    p_id = int(query.data.replace("admin_edit_div_select_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)
    if not player:
        keyboard = [[InlineKeyboardButton("« К списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    divisions = await asyncio.to_thread(database.get_divisions, is_active=1)

    keyboard = []
    curr_div_id = dict(player).get("division_id") if player else None
    if curr_div_id is None:
        keyboard.append([InlineKeyboardButton("⭐ Без дивизиона (Текущий)", callback_data=f"admin_ediv_{p_id}_none")])
    else:
        keyboard.append([InlineKeyboardButton("❌ Снять с дивизиона", callback_data=f"admin_ediv_{p_id}_none")])

    for div in divisions:
        d_id = div["id"]
        d_name = div["name"]
        if curr_div_id == d_id:
            btn_text = f"⭐ {d_name} (текущий)"
        else:
            btn_text = f"🏆 {d_name}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_ediv_{p_id}_{d_id}")])

    keyboard.append([InlineKeyboardButton("« Отмена", callback_data=f"admin_view_player_{p_id}")])

    text = (
        f"🏆 <b>Выберите дивизион для игрока {html.escape('@' + player['username'] if player['username'] else str(p_id))}:</b>\n\n"
        f"<i>(Клик по кнопке моментально назначит дивизион)</i>"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_edit_div_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute division assignment via inline button click."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return

    parts = query.data.replace("admin_ediv_", "").rsplit("_", 1)
    p_id = int(parts[0])
    target_div_raw = parts[1]
    target_div_id = None if target_div_raw == "none" else int(target_div_raw)

    await asyncio.to_thread(database.assign_user_division, p_id, target_div_id)
    await query.answer("✅ Дивизион обновлен!", show_alert=False)
    await admin_view_player(update, context, player_id=p_id)

async def _announce_player_exclusion(
    context: ContextTypes.DEFAULT_TYPE,
    division_id: int | None,
    user_id: int,
    username_str: str,
    team_str: str
) -> None:
    """
    Объявить об исключении игрока в его дивизионе и убрать его из группы.

    Уведомление уходит только игрокам дивизиона: у игрока без дивизиона нет
    группы, в которую можно написать, поэтому такой вызов молча выходит.
    Сообщение идёт в тему General (без message_thread_id), затем игрок кикается.
    """
    if not division_id:
        return

    group_id = await asyncio.to_thread(database.get_division_group_chat_id, division_id)
    if not group_id:
        logger.info(f"Player {user_id} excluded: division {division_id} has no bound group, notice skipped.")
        return

    notice_text = (
        f"📢 <b>Изменение состава лиги!</b>\n\n"
        f"Игрок <b>{html.escape(username_str)}</b> покинул клуб <b>{html.escape(team_str)}</b>.\n"
        f"Клуб свободен и ждёт нового владельца!"
    )
    try:
        # Без message_thread_id сообщение попадает в General форума.
        await context.bot.send_message(chat_id=group_id, text=notice_text, parse_mode="HTML")
    except (BadRequest, TelegramError) as e:
        logger.warning(f"Could not announce exclusion of player {user_id} in group {group_id}: {e}")

    # Админа из группы не выкидываем: исключение из лиги — не повод терять доступ.
    if is_global_admin(user_id):
        logger.info(f"Player {user_id} excluded but kept in group {group_id}: global admin.")
        return

    try:
        # Кик = бан + немедленный разбан, иначе игрок не сможет вернуться в лигу.
        await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id, only_if_banned=True)
    except (BadRequest, TelegramError) as e:
        logger.warning(f"Could not remove player {user_id} from group {group_id}: {e}")


@admin_only
async def admin_delete_player_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show confirmation screen before deleting a player."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    p_id = int(query.data.replace("admin_delete_player_confirm_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)

    if not player:
        keyboard = [[InlineKeyboardButton("« К списку", callback_data="admin_list_players_page_0")]]
        await query.edit_message_text("❌ Игрок не найден.", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    username_str = f"@{player['username']}" if player['username'] else f"ID: {p_id}"
    team_str = player['team_name'] or 'Без клуба'

    text = (
        f"⚠️ <b>Исключение игрока из лиги</b>\n\n"
        f"Вы действительно хотите исключить <b>{html.escape(username_str)}</b> (Клуб: <b>{html.escape(team_str)}</b>)?\n\n"
        f"Клуб <b>{html.escape(team_str)}</b> освободится для нового участника. Матчи останутся несыгранными."
    )
    if player["division_id"]:
        text += "\n\n🚪 Игрок будет объявлен выбывшим в своём дивизионе и удалён из группы."

    keyboard = [
        [InlineKeyboardButton("✅ Да, исключить", callback_data=f"admin_delete_player_execute_{p_id}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{p_id}")]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

@admin_only
async def admin_delete_player_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute player deletion, announce it in the player's division and kick them."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    await query.answer()

    p_id = int(query.data.replace("admin_delete_player_execute_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)

    if not player:
        await admin_list_players_page(update, context, page=0)
        return

    username_str = f"@{player['username']}" if player['username'] else f"ID: {p_id}"
    team_str = player['team_name'] or 'без названия'
    # Дивизион читаем до удаления: remove_player стирает строку пользователя.
    division_id = player["division_id"]

    success, msg = await asyncio.to_thread(database.remove_player, str(p_id))
    if success:
        try:
            await _post_or_update_debts_in_warns(context)
        except Exception as e:
            logger.warning(f"Failed to update debts in warns on player delete: {e}")

        await _announce_player_exclusion(context, division_id, p_id, username_str, team_str)

    await query.answer(f"✅ {msg}", show_alert=True)
    await admin_list_players_page(update, context, page=0)


def _roster_back_cb(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Возврат с экранов составов — к клубам своего дивизиона.

    Дивизион в сессии не сохранён (бот перезапущен, устаревшее сообщение) —
    уводим в админку: глобального экрана составов больше нет, а хаб дивизионов
    закрыт `_ensure_super_admin` и отшил бы админа дивизиона.
    """
    div_id = context.user_data.get("admin_roster_div_id") if context.user_data else None
    return f"admin_roster_div:{div_id}" if div_id else "admin_main_menu"


@admin_only
async def admin_rosters_for_division(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of clubs in the selected division for squad management."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    div_id_str = query.data.split(":", 1)[1] if ":" in query.data else ""
    try:
        div_id = int(div_id_str)
    except (ValueError, TypeError):
        div_id = 1

    context.user_data["admin_roster_div_id"] = div_id

    division = await asyncio.to_thread(database.get_division, div_id)
    div_name = division.get("name") if division else f"Дивизион #{div_id}"

    teams = await asyncio.to_thread(database.get_division_teams, div_id)

    keyboard = []
    if teams:
        row = []
        for club in teams:
            row.append(InlineKeyboardButton(club, callback_data=f"admin_squad_view_{club}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
    else:
        text_empty = f"⚠️ В дивизионе <b>{html.escape(div_name)}</b> пока нет зарегистрированных команд."
        keyboard.append([InlineKeyboardButton("« Назад в дивизион", callback_data=f"admin_div_view_{div_id}")])
        await query.edit_message_text(text_empty, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    keyboard.append([
        InlineKeyboardButton("📊 Статус составов", callback_data=f"admin_squads_view:{div_id}"),
        InlineKeyboardButton("➕ Добавить игроков из матчей", callback_data=f"admin_squad_add_missing_div:{div_id}")
    ])
    # Кэш портретов общий для всей лиги (get_all_unique_players), дивизионного
    # скоупа у него нет — подпись говорит об этом прямо.
    keyboard.append([
        InlineKeyboardButton("🖼 Загрузить фото игроков (вся лига)", callback_data="admin_fetch_photos_cb")
    ])
    keyboard.append([InlineKeyboardButton("« Назад в дивизион", callback_data=f"admin_div_view_{div_id}")])

    text = (
        f"📋 <b>Составы — {html.escape(div_name)}</b>\n\n"
        f"Всего клубов: <b>{len(teams)}</b>\n"
        f"Выберите клуб для просмотра и управления составом:"
    )
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))



@admin_only
async def admin_view_squad(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View squad for a specific club."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    club = query.data.replace("admin_squad_view_", "")
    squad_items = await asyncio.to_thread(database.get_squad_with_positions, club)

    div_id = context.user_data.get("admin_roster_div_id")
    if not div_id:
        user = await asyncio.to_thread(database.get_user_by_team, club)
        if user and user.get("division_id"):
            div_id = user["division_id"]

    back_cb = f"admin_roster_div:{div_id}" if div_id else "admin_divs_hub"

    if squad_items:
        lines = [f"👥 <b>Состав команды {html.escape(club)}:</b>\n"]
        for i, item in enumerate(squad_items, 1):
            p_name = item.get("player_name", "Игрок")
            p_pos = item.get("position", "ST")
            lines.append(f"{i}. <code>[{p_pos}]</code> <b>{html.escape(p_name)}</b>")
        text = "\n".join(lines)
    else:
        text = f"👥 <b>Состав команды {html.escape(club)}:</b>\n\n<i>Состав пуст.</i>"

    keyboard = [
        [InlineKeyboardButton("🏛 Карточка клуба", callback_data=f"view_club_{club}")],
        [
            InlineKeyboardButton("👥 Загрузить основу", callback_data=f"admin_squad_upload_{club}"),
            InlineKeyboardButton("👥 Загрузить резерв / скамейку", callback_data=f"admin_squad_upload_reserves_{club}"),
        ],
        [InlineKeyboardButton("➕ Добавить игрока", callback_data=f"admin_squad_add_player_{club}")],
        [InlineKeyboardButton("➖ Удалить игрока", callback_data=f"admin_squad_rm_menu_{club}")],
        [InlineKeyboardButton("➕ Добавить игроков из матчей", callback_data=f"admin_squad_add_missing_{club}")],
        [InlineKeyboardButton("🗑️ Очистить состав", callback_data=f"admin_squad_clear_{club}")],
        [InlineKeyboardButton("« Назад к клубам", callback_data=back_cb)]
    ]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_squad_upload_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start squad upload: ask admin to send player names."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return ConversationHandler.END

    if query.data.startswith("admin_squad_upload_reserves_"):
        club = query.data.replace("admin_squad_upload_reserves_", "")
        is_reserves = True
    else:
        club = query.data.replace("admin_squad_upload_", "")
        is_reserves = False

    context.user_data["admin_squad_club"] = club
    context.user_data["admin_squad_is_reserves"] = is_reserves

    if is_reserves:
        text = (
            f"👥 <b>Загрузка резерва (скамейки) для {html.escape(club)}</b>\n\n"
            "📸 Пришлите <b>скриншот экрана «Резервисты»</b> или списка запасных — игроки будут распознаны ИИ и добавлены к текущему составу.\n\n"
            "Либо отправьте список футболистов текстом, каждый с новой строки:\n"
            "<code>Rodrygo\n"
            "Ferland Mendy\n"
            "William Saliba</code>"
        )
    else:
        text = (
            f"📊 <b>Загрузка основы для {html.escape(club)}</b>\n\n"
            "📸 Пришлите <b>скриншот состава</b> — игроки будут распознаны ИИ.\n\n"
            "Либо отправьте список футболистов текстом, каждый с новой строки:\n"
            "<code>Viktor Gyökeres\n"
            "Francisco Trincão\n"
            "Pedro Gonçalves</code>"
        )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_squad_view_{club}")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_EXPECT_SQUAD_TEXT


@admin_only
async def admin_squad_upload_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Recognize a squad screenshot and offer to apply it to the club's roster."""
    club = context.user_data.pop("admin_squad_club", None)
    is_reserves = context.user_data.pop("admin_squad_is_reserves", False)
    if not club:
        await update.message.reply_text("❌ Ошибка: не найден клуб. Попробуйте снова.")
        return ConversationHandler.END

    await offer_recognized_squad(
        update, context,
        club=club,
        file_id=update.message.photo[-1].file_id,
        back_cb=f"admin_squad_view_{club}",
        is_reserves=is_reserves,
    )
    return ConversationHandler.END


@admin_only
async def admin_squad_upload_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive player names and add them to the squad."""
    club = context.user_data.pop("admin_squad_club", None)
    is_reserves = context.user_data.pop("admin_squad_is_reserves", False)
    if not club:
        await update.message.reply_text("❌ Ошибка: не найден клуб. Попробуйте снова.")
        return ConversationHandler.END

    lines = update.message.text.strip().split("\n")
    player_names = [html.escape(line.strip()) for line in lines if line.strip()]

    if not player_names:
        await update.message.reply_text("❌ Список пуст. Отправьте хотя бы одного игрока.")
        return ADMIN_EXPECT_SQUAD_TEXT

    added = await asyncio.to_thread(database.add_squad, club, [line.strip() for line in lines if line.strip()])

    label = "резервистов" if is_reserves else "футболистов"
    text = f"✅ Добавлено <b>{added}</b> {label} в состав команды <b>{html.escape(club)}</b>."
    keyboard = [[InlineKeyboardButton("👥 Просмотреть состав", callback_data=f"admin_squad_view_{club}")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


@admin_only
async def admin_squad_add_player_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start single-player add: ask admin for the player's name."""
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return ConversationHandler.END

    club = query.data.replace("admin_squad_add_player_", "")
    context.user_data["admin_squad_club"] = club

    text = (
        f"➕ <b>Добавление игрока в {html.escape(club)}</b>\n\n"
        "Отправьте имя футболиста одним сообщением."
    )
    keyboard = [[InlineKeyboardButton("❌ Отмена", callback_data=f"admin_squad_view_{club}")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ADMIN_EXPECT_SINGLE_PLAYER


@admin_only
async def admin_squad_add_player_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive a single player name and add it to the squad."""
    club = context.user_data.pop("admin_squad_club", None)
    if not club:
        await update.message.reply_text("❌ Ошибка: не найден клуб. Попробуйте снова.")
        return ConversationHandler.END

    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("❌ Имя пустое. Отправьте имя игрока.")
        return ADMIN_EXPECT_SINGLE_PLAYER

    added = await asyncio.to_thread(database.add_squad, club, [name])

    if added:
        text = f"✅ Игрок <b>{html.escape(name)}</b> добавлен в состав команды <b>{html.escape(club)}</b>."
    else:
        text = f"ℹ️ Игрок <b>{html.escape(name)}</b> уже есть в составе команды <b>{html.escape(club)}</b> или имя некорректно."
    keyboard = [[InlineKeyboardButton("👥 Просмотреть состав", callback_data=f"admin_squad_view_{club}")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


@admin_only
async def admin_squad_clear(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all players from a club's squad."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    club = query.data.replace("admin_squad_clear_", "")
    deleted = await asyncio.to_thread(database.clear_squad, club)

    text = f"🗑️ Состав команды <b>{html.escape(club)}</b> очищен. Удалено игроков: <b>{deleted}</b>."
    back_cb = _roster_back_cb(context)
    keyboard = [[InlineKeyboardButton("« Назад к клубам", callback_data=back_cb)]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_squad_rm_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show list of players in the club's squad with delete buttons."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    club = query.data.replace("admin_squad_rm_menu_", "")
    squad = await asyncio.to_thread(database.get_squad, club)

    if not squad:
        text = f"👥 <b>Состав команды {html.escape(club)}:</b>\n\n<i>Состав пуст.</i>"
        keyboard = [[InlineKeyboardButton("« Назад к составу", callback_data=f"admin_squad_view_{club}")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    context.user_data[f"rm_squad_{club}"] = squad

    text = (
        f"🗑️ <b>Удаление игрока из состава {html.escape(club)}</b>\n\n"
        f"Нажмите на игрока, которого хотите удалить:"
    )

    keyboard = []
    row = []
    for idx, player in enumerate(squad):
        row.append(InlineKeyboardButton(f"❌ {player}", callback_data=f"admin_squad_del_p_{club}_{idx}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("« Назад к составу", callback_data=f"admin_squad_view_{club}")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_squad_del_player(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete selected player from club squad."""
    query = update.callback_query
    if not query:
        return
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    data_parts = query.data.replace("admin_squad_del_p_", "").rsplit("_", 1)
    if len(data_parts) != 2:
        await query.answer("❌ Ошибка данных.")
        return

    club, idx_str = data_parts
    try:
        idx = int(idx_str)
    except ValueError:
        await query.answer("❌ Неверный индекс.")
        return

    squad = context.user_data.get(f"rm_squad_{club}")
    if not squad or idx >= len(squad):
        squad = await asyncio.to_thread(database.get_squad, club)

    if not squad or idx >= len(squad):
        await query.answer("❌ Игрок не найден.")
        return

    player_name = squad[idx]
    success = await asyncio.to_thread(database.remove_player_from_squad, club, player_name)

    if success:
        await query.answer(f"✅ Игрок {player_name} удален из {club}!", show_alert=False)
    else:
        await query.answer("❌ Не удалось удалить игрока.")

    new_squad = await asyncio.to_thread(database.get_squad, club)
    context.user_data[f"rm_squad_{club}"] = new_squad

    if not new_squad:
        text = f"👥 <b>Состав команды {html.escape(club)} теперь пуст.</b>"
        keyboard = [[InlineKeyboardButton("« Назад к составу", callback_data=f"admin_squad_view_{club}")]]
        await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    text = (
        f"🗑️ <b>Удаление игрока из состава {html.escape(club)}</b>\n\n"
        f"Игрок <b>{html.escape(player_name)}</b> успешно удален!\n"
        f"Выберите следующего игрока для удаления или вернитесь назад:"
    )

    keyboard = []
    row = []
    for i, player in enumerate(new_squad):
        row.append(InlineKeyboardButton(f"❌ {player}", callback_data=f"admin_squad_del_p_{club}_{i}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton("« Назад к составу", callback_data=f"admin_squad_view_{club}")])
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_squad_add_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Add players that appear in match events but are missing from a club's squad."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    data = query.data
    if data.startswith("admin_squad_add_missing_div:"):
        div_id = int(data.split(":", 1)[1])
        if not await _ensure_division_access(update, div_id):
            return
        # Обрабатываем клубы дивизиона поимённо: add_missing_squad_players()
        # без аргумента прошлась бы по всей лиге.
        teams = await asyncio.to_thread(database.get_division_teams, div_id)
        added = 0
        for club in teams:
            added += await asyncio.to_thread(database.add_missing_squad_players, club)
        text = (
            f"✅ В клубы дивизиона добавлено игроков из матчей: <b>{added}</b>.\n"
            f"Обработано клубов: <b>{len(teams)}</b>."
        )
        back_data = f"admin_roster_div:{div_id}"
    else:
        club = data.replace("admin_squad_add_missing_", "")
        missing = await asyncio.to_thread(database.get_missing_squad_players, club)
        if not missing:
            text = f"✅ В составе <b>{html.escape(club)}</b> нет игроков из матчей, отсутствующих в составе."
            back_cb = _roster_back_cb(context)
            keyboard = [[InlineKeyboardButton("« Назад к клубам", callback_data=back_cb)]]
            await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
            return
        added = await asyncio.to_thread(database.add_missing_squad_players, club)
        lines = [f"➕ Добавлено <b>{added}</b> игроков из матчей в состав <b>{html.escape(club)}</b>:\n"]
        for name in missing:
            lines.append(f"• {html.escape(name)}")
        text = "\n".join(lines)
        back_data = f"admin_squad_view_{club}"

    keyboard = [[InlineKeyboardButton("« Назад", callback_data=back_data)]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))



# ── Squad Upload Status for Divisions ────────────────────────────────────────

def _format_division_squads_status_html(status_data: dict) -> str:
    div_name = status_data["division_name"]
    total = status_data["total_clubs"]
    ready = status_data["ready_count"]
    partial = status_data["partial_count"]
    empty = status_data["empty_count"]
    vacant = status_data["vacant_count"]

    pct = int((ready / total * 100)) if total > 0 else 0

    lines = [
        f"📋 <b>Статус составов — {html.escape(div_name)}</b>\n",
        f"📊 Готовность: <b>{ready}/{total} ({pct}%)</b>",
        f"🟢 Готовы: <b>{ready}</b>  •  🟡 Неполные: <b>{partial}</b>",
        f"🔴 Без состава: <b>{empty}</b>  •  ⚪ Свободны: <b>{vacant}</b>\n",
    ]

    for item in status_data["clubs"]:
        club = html.escape(item["club"])
        status = item["status"]
        count = item["player_count"]
        user_id = item["user_id"]
        username = item["username"]

        if status == "ready":
            coach_str = f"@{html.escape(username)}" if username else f"id:{user_id}"
            lines.append(f"🟢 <b>{club}</b> — {coach_str} ({count} игр.)")
        elif status == "partial":
            coach_str = f"@{html.escape(username)}" if username else f"id:{user_id}"
            lines.append(f"🟡 <b>{club}</b> — {coach_str} (<i>{count} игр.</i>)")
        elif status == "empty":
            coach_str = f"@{html.escape(username)}" if username else f"id:{user_id}"
            lines.append(f"🔴 <b>{club}</b> — {coach_str} <i>(состав не загружен!)</i>")
        else:
            lines.append(f"⚪ <b>{club}</b> — <i>клуб свободен</i>")

    return "\n".join(lines)


def _build_division_squads_keyboard(div_id: int, all_div_ids: list[int], has_debtors: bool = True) -> InlineKeyboardMarkup:
    keyboard = []

    action_row = []
    if has_debtors:
        action_row.append(InlineKeyboardButton("📢 Напомнить должникам", callback_data=f"admin_squads_remind:{div_id}"))
    action_row.append(InlineKeyboardButton("🔄 Обновить", callback_data=f"admin_squads_view:{div_id}"))
    keyboard.append(action_row)

    if all_div_ids and len(all_div_ids) > 1:
        nav_row = []
        curr_idx = all_div_ids.index(div_id) if div_id in all_div_ids else 0
        prev_id = all_div_ids[(curr_idx - 1) % len(all_div_ids)]
        next_id = all_div_ids[(curr_idx + 1) % len(all_div_ids)]
        nav_row.append(InlineKeyboardButton(f"« Див. {prev_id}", callback_data=f"admin_squads_view:{prev_id}"))
        nav_row.append(InlineKeyboardButton("📊 Вся лига", callback_data="admin_squads_all"))
        nav_row.append(InlineKeyboardButton(f"Див. {next_id} »", callback_data=f"admin_squads_view:{next_id}"))
        keyboard.append(nav_row)

    keyboard.append([InlineKeyboardButton("« К дивизиону", callback_data=f"admin_div_view_{div_id}")])
    return InlineKeyboardMarkup(keyboard)


def _format_all_divisions_squads_summary_html(summary_data: dict) -> str:
    total_clubs = summary_data["total_clubs"]
    total_ready = summary_data["total_ready"]
    total_partial = summary_data["total_partial"]
    total_empty = summary_data["total_empty"]
    total_vacant = summary_data["total_vacant"]
    pct = int((total_ready / total_clubs * 100)) if total_clubs > 0 else 0

    lines = [
        "📋 <b>Статус составов — Вся лига</b>\n",
        f"📊 Общая готовность: <b>{total_ready}/{total_clubs} ({pct}%)</b>",
        f"🟢 Готовы: <b>{total_ready}</b>  •  🟡 Неполные: <b>{total_partial}</b>",
        f"🔴 Без состава: <b>{total_empty}</b>  •  ⚪ Свободны: <b>{total_vacant}</b>\n",
    ]

    for d in summary_data["divisions"]:
        d_name = html.escape(d["division_name"])
        d_ready = d["ready_count"]
        d_total = d["total_clubs"]
        d_empty = d["empty_count"]
        d_partial = d["partial_count"]
        d_pct = int((d_ready / d_total * 100)) if d_total > 0 else 0
        lines.append(
            f"🏆 <b>{d_name}</b>: 🟢 {d_ready}/{d_total} ({d_pct}%) | 🟡 {d_partial} | 🔴 {d_empty}"
        )

    return "\n".join(lines)


def _build_all_divisions_squads_keyboard(divisions: list[dict]) -> InlineKeyboardMarkup:
    keyboard = []
    row = []
    for d in divisions:
        did = d["id"]
        row.append(InlineKeyboardButton(f"Див. {did}", callback_data=f"admin_squads_view:{did}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([
        InlineKeyboardButton("🔄 Обновить", callback_data="admin_squads_all"),
        InlineKeyboardButton("« В админку", callback_data="admin_main_menu")
    ])
    return InlineKeyboardMarkup(keyboard)


async def _parse_target_division_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int | None:
    """Extract requested division id from context.args, text message, or chat context."""
    if context.args:
        raw = context.args[0].strip()
        m = re.search(r"\d+", raw)
        if m:
            return int(m.group())

    if update.message and update.message.text:
        text = update.message.text.strip()
        m = re.search(r"^/(?:squads_status|squads|sostavy|составы|состав)(?:@\w+)?(?:\s+(?:div_?|див_?)?(\d+))?", text, re.IGNORECASE)
        if m and m.group(1):
            return int(m.group(1))

    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    if chat and chat.type in ("group", "supergroup"):
        if thread_id:
            div_topic = await asyncio.to_thread(database.get_division_by_topic, thread_id, "drafts", chat.id)
            if not div_topic:
                div_topic = await asyncio.to_thread(database.get_division_by_topic, thread_id, group_chat_id=chat.id)
            if div_topic:
                return div_topic["id"]
        div_grp = await asyncio.to_thread(database.get_division_by_group, chat.id)
        if div_grp:
            return div_grp["id"]

    return None


async def admin_squads_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Command /squads_status, /squads, /составы: display squad upload progress."""
    user = update.effective_user
    if not user:
        return

    is_admin = is_global_admin(user.id)
    admin_divs = await asyncio.to_thread(database.get_admin_divisions, user.id)
    admin_div_ids = [d["id"] for d in admin_divs]

    if not is_admin and not admin_div_ids:
        await _deny_access(update, "⛔ Эта команда доступна только администраторам.")
        return

    div_id = await _parse_target_division_id(update, context)

    # If user is division admin of exactly one division and no div_id specified:
    if div_id is None and not is_admin and len(admin_div_ids) == 1:
        div_id = admin_div_ids[0]

    all_divs = await asyncio.to_thread(database.get_divisions, True)
    all_div_ids = [d["id"] for d in all_divs]

    if div_id is not None:
        if not is_admin and div_id not in admin_div_ids:
            await _deny_access(update, "⛔ У вас нет прав на этот дивизион.")
            return

        status_data = await asyncio.to_thread(database.get_division_squads_status, div_id)
        has_debtors = any(c["status"] in ("empty", "partial") for c in status_data["clubs"])
        text = _format_division_squads_status_html(status_data)
        markup = _build_division_squads_keyboard(div_id, all_div_ids, has_debtors=has_debtors)
        if update.message:
            await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)
        elif update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=markup)
        return

    # No div_id: show selector
    if is_admin:
        selectable_divs = all_divs
    else:
        selectable_divs = [d for d in all_divs if d["id"] in admin_div_ids]

    keyboard = []
    row = []
    for d in selectable_divs:
        row.append(InlineKeyboardButton(d["name"], callback_data=f"admin_squads_view:{d['id']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    if is_admin:
        keyboard.append([InlineKeyboardButton("📊 Сводка по всей лиге", callback_data="admin_squads_all")])
    keyboard.append([InlineKeyboardButton("« В админку", callback_data="admin_main_menu")])

    text = "📋 <b>Статус загрузки составов</b>\n\nВыберите дивизион для просмотра отчёта:"
    if update.message:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_squads_view_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback query handler: view squads status for a division."""
    query = update.callback_query
    if not query:
        return

    div_id = int(query.data.split(":", 1)[1])
    if not await _ensure_division_access(update, div_id):
        return

    all_divs = await asyncio.to_thread(database.get_divisions, True)
    all_div_ids = [d["id"] for d in all_divs]

    status_data = await asyncio.to_thread(database.get_division_squads_status, div_id)
    has_debtors = any(c["status"] in ("empty", "partial") for c in status_data["clubs"])
    text = _format_division_squads_status_html(status_data)
    markup = _build_division_squads_keyboard(div_id, all_div_ids, has_debtors=has_debtors)

    await safe_edit_or_reply(query, context, text, reply_markup=markup, parse_mode="HTML")


@admin_only
async def admin_squads_all_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Callback query handler: view summary across all divisions."""
    query = update.callback_query
    if not query:
        return

    if not is_global_admin(query.from_user.id):
        await _deny_access(update, "⛔ Сводка по всей лиге доступна только главным администраторам.")
        return

    all_divs = await asyncio.to_thread(database.get_divisions, True)
    summary_data = await asyncio.to_thread(database.get_all_divisions_squads_summary)
    text = _format_all_divisions_squads_summary_html(summary_data)
    markup = _build_all_divisions_squads_keyboard(all_divs)

    await safe_edit_or_reply(query, context, text, reply_markup=markup, parse_mode="HTML")


@admin_only
async def admin_squads_remind_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminder in DM to all coaches with empty/partial squads."""
    query = update.callback_query
    if not query:
        return

    div_id = int(query.data.split(":", 1)[1])
    if not await _ensure_division_access(update, div_id):
        return

    status_data = await asyncio.to_thread(database.get_division_squads_status, div_id)
    debtors = [c for c in status_data["clubs"] if c["status"] in ("empty", "partial") and c.get("user_id")]

    if not debtors:
        await query.answer("✅ В этом дивизионе у всех тренеров составы уже загружены!", show_alert=True)
        return

    div_name = status_data["division_name"]
    sent_count = 0
    failed_count = 0

    cabinet_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Загрузить состав", callback_data="cabinet_upload_squad")]
    ])

    for d in debtors:
        uid = d["user_id"]
        club = d["club"]
        msg_text = (
            f"⚠️ <b>Напоминание о загрузке состава</b>\n\n"
            f"Тренер, состав вашего клуба <b>{html.escape(club)}</b> "
            f"в дивизионе <b>{html.escape(div_name)}</b> ещё не загружен "
            f"(или заполнен не полностью)!\n\n"
            f"Пожалуйста, загрузите скриншот состава команды в личном кабинете бота "
            f"(/cabinet) перед стартом туров."
        )
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=msg_text,
                parse_mode="HTML",
                reply_markup=cabinet_kb
            )
            sent_count += 1
            await asyncio.sleep(0.05)
        except (Forbidden, TelegramError) as e:
            logger.warning(f"Failed to send squad reminder to user {uid}: {e}")
            failed_count += 1

    alert_msg = f"📢 Напоминания отправлены: {sent_count} из {len(debtors)} тренеров."
    if failed_count > 0:
        alert_msg += f" (Не доставлено: {failed_count})"
    await query.answer(alert_msg, show_alert=True)


@admin_only
async def admin_stub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Stub for admin features under development."""
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    keyboard = [[InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")]]
    text = "🚧 <b>В разработке</b>\n\nЭтот раздел находится в разработке."
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))

async def notify_players_rounds_opened(
    context: ContextTypes.DEFAULT_TYPE,
    round_numbers: list[int],
    deadline_text: str,
    division_id: int | None = None,
) -> None:
    """Send personal match card notifications to players when rounds are opened."""
    matches = await asyncio.to_thread(database.get_matches_in_rounds, round_numbers, division_id)
    if not matches:
        return

    is_single_round = (len(round_numbers) == 1)

    if is_single_round:
        r_num = round_numbers[0]
        for m in matches:
            p1_id = m['player1_id']
            p2_id = m['player2_id']
            p1_team = m['player1_team'] or 'неизвестно'
            p2_team = m['player2_team'] or 'неизвестно'
            p1_user = f"@{m['player1_username']}" if m['player1_username'] else p1_team
            p2_user = f"@{m['player2_username']}" if m['player2_username'] else p2_team

            instruction_text = (
                f"\n\n📌 <b>Как внести результат:</b>\n"
                f"1. Нажмите кнопку <b>📝 Ввести результат</b>.\n"
                f"2. Выберите <b>⚡ Автоматический ввод (по фото)</b>.\n"
                f"3. Отправьте боту от 1 до 3 скриншотов статистики из игры.\n"
                f"4. ИИ автоматически распознает счёт, авторов голов и ассистов.\n"
                f"5. Проверьте данные и нажмите <b>✅ Всё верно</b> — результат сразу автоматически подтверждается и заносится в турнирную таблицу лиги!"
            )

            # Home player card
            if p1_id:
                text_h = (
                    f"🏟 <b>ВАШ МАТЧ | Тур {r_num}</b>\n\n"
                    f"🏠 <b>Вы ({html.escape(p1_team)})</b> -:- <b>{html.escape(p2_team)} ({html.escape(p2_user)})</b> ✈️\n\n"
                    f"⏳ <b>Дедлайн:</b> {deadline_text}\n"
                    f"📌 <b>Статус:</b> Вы играете Дома."
                    f"{instruction_text}"
                )
                kb_h = [
                    [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{m['id']}")],
                    [InlineKeyboardButton("👀 Состав соперника", callback_data=f"cabinet_view_squad_{p2_id}")]
                ]
                await safe_send_notification(context.bot, p1_id, text_h, InlineKeyboardMarkup(kb_h))

            # Away player card
            if p2_id:
                text_a = (
                    f"🏟 <b>ВАШ МАТЧ | Тур {r_num}</b>\n\n"
                    f"🏠 <b>{html.escape(p1_team)} ({html.escape(p1_user)})</b> -:- <b>Вы ({html.escape(p2_team)})</b> ✈️\n\n"
                    f"⏳ <b>Дедлайн:</b> {deadline_text}\n"
                    f"📌 <b>Статус:</b> Вы играете в Гостях."
                    f"{instruction_text}"
                )
                kb_a = [
                    [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{m['id']}")],
                    [InlineKeyboardButton("👀 Состав соперника", callback_data=f"cabinet_view_squad_{p1_id}")]
                ]
                await safe_send_notification(context.bot, p2_id, text_a, InlineKeyboardMarkup(kb_a))

    else:
        # Multi-round combined notification per player
        player_matches = {}
        for m in matches:
            for pid in (m['player1_id'], m['player2_id']):
                if pid:
                    if pid not in player_matches:
                        player_matches[pid] = []
                    player_matches[pid].append(m)

        r_min, r_max = min(round_numbers), max(round_numbers)

        for pid, p_m_list in player_matches.items():
            lines = [
                f"🏟 <b>ВАШИ МАТЧИ В ОТКРЫТЫХ ТУРАХ (Туры {r_min}-{r_max})</b>\n",
                f"⏳ <b>Общий дедлайн:</b> {deadline_text}",
                "────────────────────────\n"
            ]

            for m in p_m_list:
                p1_team = m['player1_team'] or 'неизвестно'
                p2_team = m['player2_team'] or 'неизвестно'
                p1_user = f"@{m['player1_username']}" if m['player1_username'] else p1_team
                p2_user = f"@{m['player2_username']}" if m['player2_username'] else p2_team

                if m['player1_id'] == pid:
                    lines.append(f"📌 <b>Тур {m['round_number']} (Дома 🏠):</b>")
                    lines.append(f"🏠 <b>Вы ({html.escape(p1_team)})</b> -:- <b>{html.escape(p2_team)} ({html.escape(p2_user)})</b> ✈️\n")
                else:
                    lines.append(f"📌 <b>Тур {m['round_number']} (В гостях ✈️):</b>")
                    lines.append(f"🏠 <b>{html.escape(p1_team)} ({html.escape(p1_user)})</b> -:- <b>Вы ({html.escape(p2_team)})</b> ✈️\n")

            lines.append("────────────────────────")
            lines.append("👇 <i>Все матчи доступны в Личном кабинете в разделе «📋 Мои матчи»!</i>")

            kb = [
                [InlineKeyboardButton("📋 Перейти к матчам", callback_data="cabinet_my_matches")],
                [InlineKeyboardButton("👤 Личный кабинет", callback_data="menu_cabinet")]
            ]

            await safe_send_notification(context.bot, pid, "\n".join(lines), InlineKeyboardMarkup(kb))

async def send_round_reminders(
    context: ContextTypes.DEFAULT_TYPE, 
    round_number: int, 
    time_left_str: str | None = None,
    target_match_ids: set[int] | list[int] | None = None,
    division_id: int | None = None
) -> tuple[int, int]:
    """
    Send match reminders for unplayed matches in a round.
    Sends PM to unplayed match participants and a summary to Reports topic.
    Returns (sent_pm_count, unplayed_matches_count).
    """
    unplayed = await asyncio.to_thread(database.get_unplayed_matches_by_round, round_number, division_id)
    round_info = await asyncio.to_thread(database.get_round_info, round_number, division_id)
    deadline_text = round_info["deadline"] if round_info and round_info.get("deadline") else "не указан"

    if target_match_ids is not None:
        target_set = set(target_match_ids)
        unplayed = [m for m in unplayed if m['id'] in target_set]

    if not unplayed:
        return (0, 0)

    time_hdr = f" (Осталось: {time_left_str})" if time_left_str else ""
    pm_sent = 0

    # 1. PM to each player with unplayed match
    for m in unplayed:
        p1_id = m['player1_id']
        p2_id = m['player2_id']
        p1_team = m['player1_team'] or 'неизвестно'
        p2_team = m['player2_team'] or 'неизвестно'
        p1_user = f"@{m['player1_username']}" if m['player1_username'] else p1_team
        p2_user = f"@{m['player2_username']}" if m['player2_username'] else p2_team

        instruction_text = (
            f"\n\n📌 <b>Инструкция по внесению результата:</b>\n"
            f"1. Нажмите кнопку <b>📝 Ввести результат</b>.\n"
            f"2. Выберите <b>⚡ Автоматический ввод (по фото)</b>.\n"
            f"3. Отправьте боту от 1 до 3 скриншотов статистики из игры.\n"
            f"4. ИИ автоматически распознает счёт, авторов голов и ассистов.\n"
            f"5. Проверьте данные и нажмите <b>✅ Всё верно</b> — результат сразу автоматически подтверждается и заносится в турнирную таблицу лиги!"
        )

        if p1_id:
            text_h = (
                f"⏰ <b>НАПОМИНАНИЕ О МАТЧЕ | Тур {round_number}</b>{time_hdr}\n\n"
                f"🏠 <b>Вы ({html.escape(p1_team)})</b> -:- <b>{html.escape(p2_team)} ({html.escape(p2_user)})</b> ✈️\n\n"
                f"⏳ <b>Дедлайн:</b> {deadline_text}"
                f"{instruction_text}"
            )
            kb_h = [
                [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{m['id']}")],
                [InlineKeyboardButton("📋 Мои матчи", callback_data="cabinet_my_matches")]
            ]
            if await safe_send_notification(context.bot, p1_id, text_h, InlineKeyboardMarkup(kb_h)):
                pm_sent += 1

        if p2_id:
            text_a = (
                f"⏰ <b>НАПОМИНАНИЕ О МАТЧЕ | Тур {round_number}</b>{time_hdr}\n\n"
                f"🏠 <b>{html.escape(p1_team)} ({html.escape(p1_user)})</b> -:- <b>Вы ({html.escape(p2_team)})</b> ✈️\n\n"
                f"⏳ <b>Дедлайн:</b> {deadline_text}"
                f"{instruction_text}"
            )
            kb_a = [
                [InlineKeyboardButton("📝 Ввести результат", callback_data=f"cabinet_report_score_{m['id']}")],
                [InlineKeyboardButton("📋 Мои матчи", callback_data="cabinet_my_matches")]
            ]
            if await safe_send_notification(context.bot, p2_id, text_a, InlineKeyboardMarkup(kb_a)):
                pm_sent += 1

    # 2. Public summary to Reports Topic (strictly scoped by division)
    # If division_id is not passed, infer from unplayed matches if all share the same division
    if division_id is None and unplayed:
        div_ids = {m.get("division_id") for m in unplayed if m.get("division_id")}
        if len(div_ids) == 1:
            division_id = list(div_ids)[0]

    by_division: dict[int | None, list[dict]] = {}
    for m in unplayed:
        by_division.setdefault(m.get("division_id") or division_id, []).append(m)

    for target_div, div_matches in by_division.items():
        main_group_id, reports_topic_id = await resolve_division_target(
            target_div, "reports", "previews",
            legacy_topic_keys=("reports_topic_id",),
        )
        if not main_group_id:
            continue

        lines = [
            f"⏰ <b>НАПОМИНАНИЕ! Тур {round_number}</b>{time_hdr}\n",
            f"Несыгранные матчи ({len(div_matches)}):"
        ]
        for m in div_matches:
            p1_team = m['player1_team'] or 'неизвестно'
            p2_team = m['player2_team'] or 'неизвестно'
            p1_user = f"@{m['player1_username']}" if m['player1_username'] else p1_team
            p2_user = f"@{m['player2_username']}" if m['player2_username'] else p2_team
            lines.append(f"• 🏠 <b>{html.escape(p1_team)}</b> ({html.escape(p1_user)}) -:- <b>{html.escape(p2_team)}</b> ({html.escape(p2_user)}) ✈️")

        lines.append(f"\n🕒 <b>Дедлайн:</b> {deadline_text}")
        lines.append("Пожалуйста, поторопитесь сыграть свои матчи до истечения срока!")

        try:
            kwargs = {"chat_id": main_group_id, "text": "\n".join(lines), "parse_mode": "HTML"}
            if reports_topic_id:
                kwargs["message_thread_id"] = int(reports_topic_id)
            await context.bot.send_message(**kwargs)
        except Exception:
            logger.exception("Failed to post reminder summary to group")

    return (pm_sent, len(unplayed))

@admin_only
async def admin_remind_round(update: Update, context: ContextTypes.DEFAULT_TYPE, round_number: int | None = None) -> None:
    """Display match selection UI for sending round reminders."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    try:
        await query.answer()
    except Exception:
        pass

    if round_number is None:
        round_number = int(query.data.replace("admin_remind_round_", ""))

    target_chat_id = query.message.chat_id if query and query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    # Экран напоминаний ключуется номером тура и дивизионом из сессии
    div_id = context.user_data.get("admin_round_div_id") if context.user_data else None
    back_cb = _round_back_cb(context, round_number)

    unplayed = await asyncio.to_thread(database.get_unplayed_matches_by_round, round_number, div_id)
    if not unplayed:
        keyboard = [[InlineKeyboardButton("« Назад к туру", callback_data=back_cb)]]
        try:
            await query.edit_message_text("🎉 В этом туре нет несыгранных матчей!", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text="🎉 В этом туре нет несыгранных матчей!", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    selected_key = f"remind_selected_{div_id}_{round_number}" if div_id else f"remind_selected_{round_number}"
    if selected_key not in context.user_data:
        context.user_data[selected_key] = {m['id'] for m in unplayed}

    selected_ids = context.user_data[selected_key]

    div_header = ""
    if div_id:
        div_row = await asyncio.to_thread(database.get_division, div_id)
        if div_row and div_row.get("name"):
            div_header = f" ({html.escape(div_row['name'])})"

    text = (
        f"🔔 <b>Выбор матчей для отправки напоминаний (Тур {round_number}){div_header}</b>\n\n"
        f"Отметьте матчи участников, которым нужно отправить напоминание о дедлайне:"
    )

    keyboard = []
    for m in unplayed:
        m_id = m['id']
        is_checked = m_id in selected_ids
        icon = "✅" if is_checked else "⬜️"
        p1 = html.escape(m['player1_team'] or m['player1_nickname'] or "Хозяева")
        p2 = html.escape(m['player2_team'] or m['player2_nickname'] or "Гости")
        btn_text = f"{icon} {p1} vs {p2}"
        keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"admin_toggle_remind_match_{round_number}_{m_id}")])

    all_checked = (len(selected_ids) == len(unplayed))
    toggle_all_btn = "⏹ Снять все" if all_checked else "☑️ Выбрать все"
    keyboard.append([InlineKeyboardButton(toggle_all_btn, callback_data=f"admin_toggle_remind_all_{round_number}")])

    count_selected = len(selected_ids)
    if count_selected > 0:
        keyboard.append([InlineKeyboardButton(f"🚀 Отправить напоминания ({count_selected})", callback_data=f"admin_send_selected_reminders_{round_number}")])

    keyboard.append([InlineKeyboardButton("« Назад к туру", callback_data=back_cb)])
    markup = InlineKeyboardMarkup(keyboard)

    if query.message and query.message.photo:
        try:
            await query.message.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")
    else:
        try:
            await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            try:
                await query.message.delete()
            except Exception:
                pass
            await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")

@admin_only
async def admin_toggle_remind_match(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle a single match selection for reminder dispatch."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    try:
        await query.answer()
    except Exception:
        pass

    parts = query.data.replace("admin_toggle_remind_match_", "").split("_")
    if len(parts) != 2:
        return
    round_number = int(parts[0])
    match_id = int(parts[1])

    div_id = context.user_data.get("admin_round_div_id") if context.user_data else None
    selected_key = f"remind_selected_{div_id}_{round_number}" if div_id else f"remind_selected_{round_number}"
    unplayed = await asyncio.to_thread(database.get_unplayed_matches_by_round, round_number, div_id)
    unplayed_ids = {m['id'] for m in unplayed}

    selected_ids = context.user_data.setdefault(selected_key, set(unplayed_ids))

    if match_id in selected_ids:
        selected_ids.remove(match_id)
    else:
        selected_ids.add(match_id)

    context.user_data[selected_key] = selected_ids
    await admin_remind_round(update, context, round_number=round_number)

@admin_only
async def admin_toggle_remind_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle select all / deselect all matches for reminder dispatch."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    try:
        await query.answer()
    except Exception:
        pass

    round_number = int(query.data.replace("admin_toggle_remind_all_", ""))
    div_id = context.user_data.get("admin_round_div_id") if context.user_data else None
    unplayed = await asyncio.to_thread(database.get_unplayed_matches_by_round, round_number, div_id)
    unplayed_ids = {m['id'] for m in unplayed}

    selected_key = f"remind_selected_{div_id}_{round_number}" if div_id else f"remind_selected_{round_number}"
    selected_ids = context.user_data.get(selected_key, set())

    if len(selected_ids) == len(unplayed_ids):
        context.user_data[selected_key] = set()
    else:
        context.user_data[selected_key] = set(unplayed_ids)

    await admin_remind_round(update, context, round_number=round_number)

@admin_only
async def admin_send_selected_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send reminders to only the selected matches."""
    query = update.callback_query
    if not query or not is_admin(query.from_user.id):
        return
    try:
        await query.answer()
    except Exception:
        pass

    target_chat_id = query.message.chat_id if query and query.message else query.from_user.id
    thread_id = query.message.message_thread_id if query and query.message and query.message.is_topic_message else None

    round_number = int(query.data.replace("admin_send_selected_reminders_", ""))
    div_id = context.user_data.get("admin_round_div_id") if context.user_data else None
    selected_key = f"remind_selected_{div_id}_{round_number}" if div_id else f"remind_selected_{round_number}"
    selected_ids = context.user_data.get(selected_key, set())

    if not selected_ids:
        await query.answer("⚠️ Не выбрано ни одного матча!", show_alert=True)
        return

    pm_sent, count_matches = await send_round_reminders(context, round_number, target_match_ids=selected_ids, division_id=div_id)

    context.user_data.pop(selected_key, None)

    text = (
        f"✅ <b>Напоминания успешно отправлены!</b>\n\n"
        f"🏟 Выбранных матчей: {count_matches}\n"
        f"📨 Игроков оповещено в ЛС: {pm_sent}"
    )

    back_cb = _round_back_cb(context, round_number)
    keyboard = [[InlineKeyboardButton("« Вернуться к туру", callback_data=back_cb)]]
    markup = InlineKeyboardMarkup(keyboard)

    try:
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await context.bot.send_message(chat_id=target_chat_id, message_thread_id=thread_id, text=text, reply_markup=markup, parse_mode="HTML")

async def job_check_deadlines_and_remind(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Напоминания о дедлайне открытых туров и сигнал админам, что тур пора закрыть.

    Вехи — `config.ROUND_DEADLINE_REMINDER_HOURS`, выбор — `debt_policy.plan_deadline_reminder`.
    Когда дедлайн прошёл, тур сам не закрывается: админы дивизиона получают
    одно сообщение с кнопкой закрытия (тег `deadline_passed_admin`).
    """
    open_rounds = await asyncio.to_thread(database.get_open_rounds_with_deadlines)
    if not open_rounds:
        return

    now = now_msk()
    for r in open_rounds:
        try:
            r_num = r["round_number"]
            div_id = r.get("division_id") or 1
            dl_dt = database.parse_flexible_datetime(r["deadline"])
            if not dl_dt:
                continue

            hours_left = (dl_dt - now).total_seconds() / 3600.0
            sent = await asyncio.to_thread(database.get_sent_reminder_tags, r_num, div_id)

            if hours_left <= 0:
                if "deadline_passed_admin" not in sent:
                    await _notify_admins_round_awaits_close(context, r_num, div_id, r["deadline"])
                    await asyncio.to_thread(database.record_reminder_sent, r_num, "deadline_passed_admin", div_id)
                continue

            plan = debt_policy.plan_deadline_reminder(hours_left, sent)
            if plan is None:
                continue
            milestone, tags = plan
            await send_round_reminders(
                context, r_num,
                time_left_str=debt_policy.deadline_reminder_label(milestone, hours_left),
                division_id=div_id,
            )
            await asyncio.to_thread(database.record_reminders_sent, r_num, tags, div_id)
        except Exception as e:
            logger.exception(f"Error checking deadline reminder for round {r.get('round_number')} div {r.get('division_id')}: {e}")


async def _notify_admins_round_awaits_close(
    context: ContextTypes.DEFAULT_TYPE, round_number: int, division_id: int, deadline: str
) -> None:
    """Дедлайн тура прошёл: админам — сколько матчей не сыграно и кнопка закрытия."""
    preview = await asyncio.to_thread(database.preview_close_round, round_number, division_id)
    div_name = await _division_display_name(division_id)
    pending = len(preview.get("matches") or [])
    text = (
        f"🟠 <b>Дедлайн {round_number}-го тура прошёл</b> — {html.escape(div_name)}\n"
        f"Дедлайн: {html.escape(str(deadline))}\n\n"
    )
    text += (
        f"Не сыграно матчей: <b>{pending}</b> — они уже долги, отсчёт идёт от дедлайна.\n"
        if pending else "Все матчи сыграны.\n"
    )
    text += "Закройте тур, когда будете готовы: результаты долгов принимаются и после закрытия."
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔴 Закрыть тур", callback_data=f"admin_div_round_close:{division_id}:{round_number}")],
        [InlineKeyboardButton("📅 Карточка тура", callback_data=f"admin_div_round:{division_id}:{round_number}")],
    ])
    for admin_id in await _resolve_debt_admins(division_id):
        try:
            await safe_send_notification(context.bot, admin_id, text, reply_markup=keyboard)
        except Exception as e:
            logger.warning(f"Failed to notify admin {admin_id} that round {round_number} awaits closing: {e}")


async def job_post_debts_to_warns(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job (every 12 hours) posting/updating the debts summary in the ПРЕДЫ thread."""
    await _post_or_update_debts_in_warns(context)


# ─── Round analytics: превью тура и итоги тура в топик АНАЛИТИКА ────────────
#
# Обе публикации одноразовые: факт отправки пишется в round_content_posts
# (а не в round_reminders — тот чистится при каждой установке дедлайна).

async def _resolve_analytics_topic(division_id: int) -> tuple[int, int] | None:
    """(group_chat_id, message_thread_id) топика АНАЛИТИКА дивизиона или None."""
    from services.topic_cache import topic_cache

    div_topic = topic_cache.get_by_division(division_id, "analytics")
    if not div_topic:
        topics_map = await asyncio.to_thread(database.get_division_topics_map, division_id)
        div_topic = topics_map.get("analytics")

    if not div_topic or not div_topic.get("group_chat_id") or not div_topic.get("message_thread_id"):
        return None
    return int(div_topic["group_chat_id"]), int(div_topic["message_thread_id"])


async def post_round_preview(
    context: ContextTypes.DEFAULT_TYPE,
    division_id: int,
    round_number: int,
    season_id: int | None = None,
    force: bool = False,
) -> bool:
    """Собрать и опубликовать превью тура. Возвращает True, если пост ушёл."""
    from services import round_preview

    if not force and await asyncio.to_thread(database.has_round_content_post, division_id, round_number, "preview"):
        return False

    topic = await _resolve_analytics_topic(division_id)
    if not topic:
        logger.info(f"Round preview skipped: division {division_id} has no АНАЛИТИКА topic bound.")
        return False
    group_id, topic_id = topic

    payload = await asyncio.to_thread(round_preview.build_preview_payload, division_id, round_number, season_id)
    if not payload.get("fixtures"):
        logger.info(f"Round preview skipped: division {division_id} round {round_number} has no fixtures.")
        return False

    text = await asyncio.to_thread(round_preview.generate_preview_text, payload)

    try:
        msg = await context.bot.send_message(
            chat_id=group_id, text=text, parse_mode="HTML", message_thread_id=topic_id
        )
    except (BadRequest, TelegramError) as e:
        logger.warning(f"Could not post round {round_number} preview to division {division_id}: {e}")
        return False

    await asyncio.to_thread(
        database.record_round_content_post, division_id, round_number, "preview", msg.message_id
    )
    return True


def _build_potr_card_data(payload: dict) -> dict | None:
    """
    Собрать данные для EA FC карточки игрока тура.

    Карточка строится на СЕЗОННЫХ цифрах игрока: по статистике одного тура
    рейтинг вышел бы заниженным и почти всегда давал бы обычный стиль,
    что занижает игрока. Показатели самого тура уходят в подпись.
    Синхронная функция — вызывать через asyncio.to_thread.
    """
    potr = payload.get("player_of_the_round") or {}
    player_name = (potr.get("player_name") or "").strip()
    team_name = (potr.get("team_name") or "").strip()
    if not player_name or not team_name:
        return None

    try:
        stats = database.get_player_card_stats(player_name, team_name) or {}
    except Exception:
        logger.exception(f"Could not load season stats for player of the round '{player_name}' ({team_name})")
        stats = {}

    card_data = dict(stats)
    card_data.setdefault("player_name", player_name)
    card_data.setdefault("team_name", team_name)
    # Сезонных событий может не быть только при рассинхроне — тогда падаем на цифры тура.
    if not card_data.get("total_goals") and not card_data.get("total_assists"):
        card_data["total_goals"] = int(potr.get("goals") or 0)
        card_data["total_assists"] = int(potr.get("assists") or 0)

    # Дивизион нужен карточке для подписи в подвале.
    card_data["division_id"] = payload.get("division_id")
    card_data["division_name"] = payload.get("division_name")
    return card_data


async def _post_player_of_the_round_card(
    context: ContextTypes.DEFAULT_TYPE,
    group_id: int,
    topic_id: int | None,
    payload: dict,
) -> bool:
    """Анимированная карточка игрока тура вслед за итогами. True, если ушла."""
    from services.animation_sender import send_high_quality_animation
    from services.graphics.fc_card_generator import (
        calculate_fut_attributes,
        generate_animated_ea_fc_card,
        get_kpl_tier_by_ovr,
    )

    card_data = await asyncio.to_thread(_build_potr_card_data, payload)
    if not card_data:
        return False

    ovr = card_data.get("ovr") or calculate_fut_attributes(card_data)["ovr"]
    tier = get_kpl_tier_by_ovr(ovr)

    buf = await asyncio.to_thread(generate_animated_ea_fc_card, card_data, tier)

    potr = payload.get("player_of_the_round") or {}
    round_goals = int(potr.get("goals") or 0)
    round_assists = int(potr.get("assists") or 0)
    caption = (
        f"🏅 <b>ИГРОК {payload.get('round_number')} ТУРА</b>\n"
        f"<b>{html.escape(str(card_data['player_name']))}</b> · "
        f"{html.escape(str(card_data['team_name']))}\n"
        f"В туре: {round_goals}+{round_assists} · "
        f"за сезон: {int(card_data.get('total_goals') or 0)}+{int(card_data.get('total_assists') or 0)}"
    )

    await send_high_quality_animation(
        context.bot,
        group_id,
        buf,
        caption=caption,
        parse_mode="HTML",
        filename=f"potr_{tier}.mp4",
        message_thread_id=topic_id,
    )
    return True


async def post_round_digest(
    context: ContextTypes.DEFAULT_TYPE,
    division_id: int,
    round_number: int,
    season_id: int | None = None,
    force: bool = False,
) -> bool:
    """Собрать и опубликовать итоги тура (картинка + подпись). True, если пост ушёл."""
    from services import round_preview
    from services.graphics.round_digest_generator import generate_round_digest_image

    if not force and await asyncio.to_thread(database.has_round_content_post, division_id, round_number, "digest"):
        return False

    topic = await _resolve_analytics_topic(division_id)
    if not topic:
        logger.info(f"Round digest skipped: division {division_id} has no АНАЛИТИКА topic bound.")
        return False
    group_id, topic_id = topic

    payload = await asyncio.to_thread(round_preview.build_digest_payload, division_id, round_number, season_id)
    if not payload.get("results"):
        logger.info(f"Round digest skipped: division {division_id} round {round_number} has no confirmed matches.")
        return False

    img_buf = await asyncio.to_thread(generate_round_digest_image, payload)
    caption = await asyncio.to_thread(round_preview.generate_digest_caption, payload)

    try:
        msg = await context.bot.send_photo(
            chat_id=group_id, photo=img_buf, caption=caption,
            parse_mode="HTML", message_thread_id=topic_id
        )
    except (BadRequest, TelegramError) as e:
        logger.warning(f"Could not post round {round_number} digest to division {division_id}: {e}")
        return False

    await asyncio.to_thread(
        database.record_round_content_post, division_id, round_number, "digest", msg.message_id
    )

    # Карточка игрока тура — довесок к итогам. Её падение (нет ffmpeg, нет
    # игрока, Telegram отказал) не должно отменять уже опубликованный дайджест.
    try:
        await _post_player_of_the_round_card(context, group_id, topic_id, payload)
    except Exception:
        logger.exception(
            f"Could not post player-of-the-round card for division {division_id} round {round_number}"
        )

    return True


async def job_post_round_preview(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодический джоб: превью только что открытых туров в топик АНАЛИТИКА."""
    pending = await asyncio.to_thread(database.get_rounds_pending_preview)
    for r in pending:
        try:
            await post_round_preview(
                context,
                division_id=r.get("division_id") or 1,
                round_number=r["round_number"],
                season_id=r.get("season_id"),
            )
        except Exception:
            logger.exception(
                f"Round preview job failed for division {r.get('division_id')} round {r.get('round_number')}"
            )


async def job_post_round_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Периодический джоб: итоги сыгранных/закрытых туров в топик АНАЛИТИКА."""
    pending = await asyncio.to_thread(database.get_rounds_pending_digest)
    for r in pending:
        try:
            await post_round_digest(
                context,
                division_id=r.get("division_id") or 1,
                round_number=r["round_number"],
                season_id=r.get("season_id"),
            )
        except Exception:
            logger.exception(
                f"Round digest job failed for division {r.get('division_id')} round {r.get('round_number')}"
            )


# Prevents concurrent runs (scheduled tick + manual /check_debts trigger)
# from double-issuing auto-warns for the same overdue match.
_debt_tracker_lock = asyncio.Lock()


async def _resolve_debt_admins(division_id: int | None) -> list[int]:
    """Admins who must decide the fate of a debt match.

    Division admins first; if the division has none (or the match is not bound
    to a division), fall back to the global admins from `config.ADMIN_IDS` so a
    debt never sits unjudged.
    """
    admins: list[int] = []
    if division_id:
        try:
            admins = list(await asyncio.to_thread(database.get_division_admins, division_id))
        except Exception as e:
            logger.warning(f"Failed to load admins for division {division_id}: {e}")
            admins = []
    if not admins:
        admins = [int(a) for a in (config.ADMIN_IDS or [])]
    # Keep order, drop duplicates.
    seen: set[int] = set()
    return [a for a in admins if a and not (a in seen or seen.add(a))]


def _debt_deadline_str(m: dict) -> str:
    esc = m.get("escalate_at")
    return esc.strftime("%d.%m.%Y %H:%M") if isinstance(esc, datetime.datetime) else "—"


async def _escalate_debt_to_admin(
    context: ContextTypes.DEFAULT_TYPE,
    m: dict,
    *,
    recipients: list[int] | None = None,
    repeat: bool = False,
) -> bool:
    """Карточка вердикта по долгу: админам дивизиона (или `recipients`).

    Возвращает True, если карточку получил хотя бы один админ — только тогда
    трекер отмечает этап в `match_debts`, иначе повторит на следующем прогоне.
    """
    m_id = m["id"]
    rn = m.get("round_number", "?")
    t1 = html.escape(m.get("player1_team") or "Хозяева")
    t2 = html.escape(m.get("player2_team") or "Гости")
    u1 = f"@{html.escape(m['p1_username'])}" if m.get("p1_username") else t1
    u2 = f"@{html.escape(m['p2_username'])}" if m.get("p2_username") else t2
    hours = int(m.get("hours_overdue", 0.0))

    div_id = m.get("division_id")
    div_name = ""
    if div_id:
        try:
            div = await asyncio.to_thread(database.get_division, div_id)
            if div:
                div_name = f" — {html.escape(str(div['name']))}"
        except Exception:
            pass

    title = "ДОЛГ ВСЁ ЕЩЁ БЕЗ ВЕРДИКТА" if repeat else "ДОЛГ: ТРЕБУЕТСЯ ВЕРДИКТ"
    text = (
        f"⚖️ <b>{title}</b>{div_name}\n\n"
        f"🏆 <b>{rn}-й тур</b> · матч #{m_id}\n"
        f"🏠 <b>{t1}</b> ({u1})\n"
        f"✈️ <b>{t2}</b> ({u2})\n"
        f"⏳ Долг идёт: <b>{hours}ч</b>\n"
        f"⌛ Срок отыгрыша истёк: <b>{_debt_deadline_str(m)}</b>\n\n"
        f"<i>Матч не сыгран в срок. Выберите решение:</i>\n"
        f"• <b>ТП</b> — победителю +3 очка и −1 варн за долг, виновнику +1 варн.\n"
        f"• <b>ТН 0:0</b> — по 1 очку каждому и <b>по +1 варну обоим</b>.\n"
        f"• <b>Продлить</b> — долг замораживается, ставки остаются в игре.\n\n"
        f"💰 <i>При любом ТП/ТН все ставки на матч возвращаются игрокам (кэф 1.00).</i>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🏆 ТП 1:0 ({t1})", callback_data=f"admin_tp_home_{m_id}")],
        [InlineKeyboardButton(f"🏆 ТП 0:1 ({t2})", callback_data=f"admin_tp_away_{m_id}")],
        [InlineKeyboardButton("🤝 ТН 0:0 (по 1 очку)", callback_data=f"admin_tp_draw_{m_id}")],
        [InlineKeyboardButton("⏸ Продлить матч", callback_data=f"admin_extend_menu_{m_id}")],
        [InlineKeyboardButton("⚽️ Карточка матча", callback_data=f"admin_view_match_{m_id}")],
    ])

    if recipients is None:
        recipients = await _resolve_debt_admins(div_id)
    delivered = False
    for admin_id in recipients:
        try:
            await context.bot.send_message(
                chat_id=admin_id, text=text, reply_markup=keyboard, parse_mode="HTML"
            )
            delivered = True
        except Exception as e:
            logger.warning(f"Failed to escalate debt match #{m_id} to admin {admin_id}: {e}")
    return delivered


async def _global_escalation_recipients(division_id: int | None) -> list[int]:
    """Глобальные админы, которые ещё не получали карточку как админы дивизиона."""
    already = set(await _resolve_debt_admins(division_id))
    seen: set[int] = set()
    out: list[int] = []
    for a in (config.ADMIN_IDS or []):
        a = int(a)
        if a and a not in already and a not in seen:
            seen.add(a)
            out.append(a)
    return out


def _debt_dm_text(kind: str, m: dict, warns: int) -> str:
    """Одно ЛС о долге для обоих участников: первое, повторное и мягкое предупреждение."""
    rn = m.get("round_number", "?")
    t1 = html.escape(m.get("player1_team") or "Команда 1")
    t2 = html.escape(m.get("player2_team") or "Команда 2")
    u1 = f"@{html.escape(m['p1_username'])}" if m.get("p1_username") else t1
    u2 = f"@{html.escape(m['p2_username'])}" if m.get("p2_username") else t2
    left = max(0, int(m.get("hours_to_escalation") or 0))
    headers = {
        debt_lifecycle.NOTIFY: "⏳ <b>Матч стал долгом!</b>",
        debt_lifecycle.REMIND: "⏰ <b>Напоминание о несыгранном долге!</b>",
        debt_lifecycle.SOFT_WARN: "🔔 <b>Долг всё ещё не сыгран — меньше суток до вердикта.</b>",
    }
    grace = int(m.get("grace_hours") or 0)
    term = f"{config.DEBT_ESCALATION_HOURS}+{grace}ч" if grace else f"{config.DEBT_ESCALATION_HOURS}ч"
    return (
        f"{headers.get(kind, headers[debt_lifecycle.REMIND])}\n\n"
        f"🏆 <b>{rn}-й тур:</b> 🏠 <b>{t1}</b> ({u1}) 🆚 ✈️ <b>{t2}</b> ({u2})\n"
        f"⏱ Срок отыгрыша ({term}): до <b>{_debt_deadline_str(m)}</b>"
        + (f" — осталось <b>{debt_policy.hours_label(left)}</b>\n" if left > 0 else "\n")
        + f"📊 Ваши текущие варны: <b>{warns}/{MAX_WARNS_LIMIT}</b>\n\n"
        f"🎁 <i>Сыгранный долг списывает варн за долг (−1 варн).</i>\n"
        f"📸 <i>Соперник игнорирует — отправьте пруфы переписки админу дивизиона, ТП получит виновник.</i>\n"
        f"🤝 <i>Если оба молчат — после срока ТН 0:0 и <b>по +1 варну обоим</b>.</i>\n"
        f"⚠️ <i>При {MAX_WARNS_LIMIT}/{MAX_WARNS_LIMIT} варнах участник исключается из лиги.</i>"
    )


async def _debt_participants(m: dict) -> list[dict]:
    """Живые участники долга (id + варны). Клуб без владельца просто пропускается."""
    out: list[dict] = []
    for side in ("1", "2"):
        pid = m.get(f"player{side}_id")
        user = await asyncio.to_thread(database.get_user, pid) if pid and pid > 0 else None
        if not user and m.get(f"player{side}_team"):
            user = await asyncio.to_thread(
                database.find_user_by_team, m.get(f"player{side}_team"), m.get("division_id")
            )
        user = dict(user) if user else None
        if not user or not user.get("telegram_id") or int(user["telegram_id"]) <= 0:
            continue
        out.append({"id": int(user["telegram_id"]), "warns": int(user.get("warn_count") or 0)})
    return out


async def job_debt_lifecycle_tracker(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Трекер долгов: план — `services.debt_lifecycle`, отметки — `match_debts`.

    Первое ЛС участникам, повтор каждые DEBT_REMINDER_INTERVAL_HOURS, мягкое
    предупреждение за DEBT_SOFT_WARNING_HOURS до срока, карточка вердикта админам
    дивизиона в `escalate_at`, повтор каждые DEBT_REESCALATION_INTERVAL_HOURS и
    глобальным админам через DEBT_GLOBAL_ESCALATION_DELAY_HOURS. Варн даёт только
    вердикт. Продлённый матч заморожен; истёкшее продление снимается здесь.
    """
    if _debt_tracker_lock.locked():
        logger.info("Debt tracker run skipped: another run is already in progress.")
        return
    async with _debt_tracker_lock:
        await _run_debt_lifecycle_tracker(context)


async def _run_debt_lifecycle_tracker(context: ContextTypes.DEFAULT_TYPE) -> None:
    # Строки долгов заводятся здесь для туров, чей дедлайн прошёл без
    # закрытия, и закрываются для матчей, результат которых подтверждён.
    try:
        await asyncio.to_thread(database.sync_match_debts)
    except Exception:
        logger.exception("sync_match_debts failed; tracker continues on the round-derived view")
    overdue_matches = await asyncio.to_thread(database.get_detailed_overdue_matches)
    if not overdue_matches:
        return

    logger.info(f"Checking debt tracker: {len(overdue_matches)} overdue matches found")
    for m in overdue_matches:
        try:
            await _process_debt(context, m)
        except Exception:
            logger.exception(f"Debt tracker failed on match #{m.get('id')}")


async def _process_debt(context: ContextTypes.DEFAULT_TYPE, m: dict) -> None:
    """Исполнить план `debt_lifecycle.plan_debt_actions` для одного долга."""
    m_id = m["id"]
    if not m.get("debt"):
        # Без строки match_debts отметить этап некуда — иначе ЛС уходили бы
        # каждые 30 минут. Строку заведёт следующий sync_match_debts.
        logger.warning(f"Debt match #{m_id} has no match_debts row; skipped")
        return

    now = now_msk()
    actions = debt_lifecycle.plan_debt_actions(m, now)
    if debt_lifecycle.EXPIRE_EXTENSION in actions:
        await asyncio.to_thread(database.expire_match_extension, m_id)
        # Срок пересчитывается с учётом закрытой заморозки.
        fresh = await asyncio.to_thread(database.get_detailed_overdue_matches, m.get("division_id"))
        m = next((x for x in fresh if x["id"] == m_id), None)
        if m is None or not m.get("debt"):
            return
        actions = debt_lifecycle.plan_debt_actions(m, now)

    for action in actions:
        if action in (debt_lifecycle.NOTIFY, debt_lifecycle.REMIND, debt_lifecycle.SOFT_WARN):
            players = await _debt_participants(m)
            if len(players) < 2:
                logger.warning(f"Debt match #{m_id}: {2 - len(players)} side(s) without an owner")
            delivered = not players  # некому писать — этап считается пройденным
            for pl in players:
                try:
                    await context.bot.send_message(
                        chat_id=pl["id"], text=_debt_dm_text(action, m, pl["warns"]), parse_mode="HTML"
                    )
                    delivered = True
                except Exception as e:
                    logger.warning(f"Failed to DM debt #{m_id} to {pl['id']}: {e}")
            if delivered:
                stage = "soft_warned" if action == debt_lifecycle.SOFT_WARN else "reminded"
                await asyncio.to_thread(database.mark_debt_stage, m_id, stage, now)
        elif action == debt_lifecycle.ESCALATE:
            if await _escalate_debt_to_admin(context, m):
                await asyncio.to_thread(database.mark_debt_stage, m_id, "escalated", now)
        elif action == debt_lifecycle.REESCALATE:
            if await _escalate_debt_to_admin(context, m, repeat=True):
                await asyncio.to_thread(database.mark_debt_stage, m_id, "reescalated", now)
        elif action == debt_lifecycle.ESCALATE_GLOBAL:
            recipients = await _global_escalation_recipients(m.get("division_id"))
            if not recipients or await _escalate_debt_to_admin(
                context, m, recipients=recipients, repeat=True
            ):
                await asyncio.to_thread(database.mark_debt_stage, m_id, "global_escalated", now)


@admin_only
async def admin_check_debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin diagnostic command /check_debts: triggers debt check immediately and outputs full summary."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return

    overdue_matches = await asyncio.to_thread(database.get_detailed_overdue_matches)

    now = now_msk()

    lines = [
        f"🔍 <b>Диагностика системы долгов</b>\n",
        f"📅 Текущее время (МСК): <b>{now.strftime('%d.%m.%Y %H:%M:%S')}</b>",
        f"⚙️ Статус авто-варнов: <b>🟢 АКТИВНЫ</b>",
        f"📊 Найдено матчей-долгов: <b>{len(overdue_matches)}</b>\n",
    ]

    if overdue_matches:
        for idx, m in enumerate(overdue_matches[:15], 1):
            t1 = m.get('player1_team') or 'Команда 1'
            t2 = m.get('player2_team') or 'Команда 2'
            rn = m.get('round_number', '?')
            hrs = m.get('hours_overdue', 0.0)
            u1 = f"@{m.get('p1_username')}" if m.get('p1_username') else f"ID {m.get('player1_id')}"
            u2 = f"@{m.get('p2_username')}" if m.get('p2_username') else f"ID {m.get('player2_id')}"
            ext = " ⏸ (Продлен)" if m.get('is_extended') else ""
            lines.append(f"{idx}. <b>Тур {rn}</b>: {t1} ({u1}) vs {t2} ({u2}) — ⏳ <b>{hrs:.1f}ч</b>{ext}")
        if len(overdue_matches) > 15:
            lines.append(f"\n<i>...и еще {len(overdue_matches) - 15} матчей</i>")
    else:
        lines.append("⚠️ <i>В базе данных не обнаружено просроченных матчей.</i>")

    lines.append("\n🔄 <i>Запуск процесса проверки долгов...</i>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")

    # Trigger job right now
    await job_debt_lifecycle_tracker(context)


async def _admin_divisions_for(user_id: int) -> list[dict]:
    """Дивизионы, которыми админ вправе управлять (глобальный админ — все активные)."""
    if is_global_admin(user_id):
        return await asyncio.to_thread(database.get_active_divisions)
    return await asyncio.to_thread(database.get_admin_divisions, user_id)


async def _admin_round_content_command(update: Update, context: ContextTypes.DEFAULT_TYPE, content_type: str) -> None:
    """Общая реализация /round_preview и /round_digest: ручной прогон публикации."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return

    label = "Превью тура" if content_type == "preview" else "Итоги тура"
    poster = post_round_preview if content_type == "preview" else post_round_digest
    pending_fetch = (
        database.get_rounds_pending_preview if content_type == "preview" else database.get_rounds_pending_digest
    )

    explicit_round = None
    if context.args and str(context.args[0]).strip().lstrip("-").isdigit():
        explicit_round = int(str(context.args[0]).strip())

    divisions = await _admin_divisions_for(user_id)
    if not divisions:
        await update.message.reply_text("⚠️ Нет дивизионов, доступных для управления.")
        return
    allowed_ids = {d["id"] for d in divisions}

    targets: list[tuple[int, int]] = []
    if explicit_round is not None:
        targets = [(div_id, explicit_round) for div_id in sorted(allowed_ids)]
    else:
        pending = await asyncio.to_thread(pending_fetch)
        targets = [
            ((r.get("division_id") or 1), r["round_number"])
            for r in pending
            if (r.get("division_id") or 1) in allowed_ids
        ]

    if not targets:
        await update.message.reply_text(f"ℹ️ {label}: нечего публиковать — всё уже отправлено.")
        return

    await update.message.reply_text(f"🔄 <i>{label}: запускаю публикацию ({len(targets)})...</i>", parse_mode="HTML")

    sent, skipped = 0, []
    for div_id, round_number in targets:
        try:
            ok = await poster(
                context, division_id=div_id, round_number=round_number,
                force=(explicit_round is not None),
            )
            if ok:
                sent += 1
            else:
                skipped.append(f"дивизион {div_id}, тур {round_number}")
        except Exception as e:
            logger.exception(f"Manual {content_type} failed for division {div_id} round {round_number}")
            skipped.append(f"дивизион {div_id}, тур {round_number} — ошибка: {e}")

    lines = [f"✅ <b>{label}</b>: опубликовано {sent} из {len(targets)}."]
    if skipped:
        lines.append("\n<i>Пропущено:</i>")
        lines.extend(f"• {html.escape(s)}" for s in skipped[:10])
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def admin_round_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/round_preview [номер тура] — опубликовать превью тура в топик АНАЛИТИКА."""
    await _admin_round_content_command(update, context, "preview")


async def admin_round_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/round_digest [номер тура] — опубликовать итоги тура в топик АНАЛИТИКА."""
    await _admin_round_content_command(update, context, "digest")


@admin_only
async def admin_set_squad_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the topic where squads will be sent."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return
        
    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри топика (ветки), куда хотите получать составы.")
        return
        
    await asyncio.to_thread(database.set_config, "squad_topic_id", str(thread_id))
    await update.message.reply_text(f"✅ Топик для составов успешно установлен (ID: {thread_id}). Теперь составы будут присылаться сюда.")

@admin_only
async def admin_set_drafts_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the topic for receiving match results (drafts)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return
        
    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри топика «Черновик», куда игроки будут присылать результаты.")
        return
        
    await asyncio.to_thread(database.set_config, "drafts_topic_id", str(thread_id))
    await update.message.reply_text(f"✅ Тема «Черновик» успешно установлена (ID: {thread_id}). Бот будет распознавать результаты здесь!")

@admin_only
async def admin_set_reports_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the topic for reports/announcements."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return
        
    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри топика «Отчёты», куда хотите получать важные уведомления.")
        return
        
    await asyncio.to_thread(database.set_config, "reports_topic_id", str(thread_id))
    await update.message.reply_text(
        f"✅ Тема «Отчёты» установлена (ID: {thread_id}).\n\n"
        f"💡 <i>Примечание:</i> Для разделения топиков по дивизионам используйте команду /назначить_топик.",
        parse_mode="HTML"
    )

@admin_only
async def admin_set_results_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the topic for match results (legacy compatibility)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return
        
    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри топика «Результаты», куда хотите получать результаты матчей.")
        return
        
    await asyncio.to_thread(database.set_config, "results_topic_id", str(thread_id))
    await update.message.reply_text(
        f"✅ Тема «Результаты» установлена (ID: {thread_id}).\n\n"
        f"💡 <i>Примечание:</i> Для разделения топиков по дивизионам используйте команду /назначить_топик.",
        parse_mode="HTML"
    )


@admin_only
async def admin_set_warns_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Set the topic for warnings (legacy compatibility)."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return

    thread_id = update.message.message_thread_id
    if not thread_id:
        await update.message.reply_text("⚠️ Вызовите команду внутри топика «ПРЕДЫ», куда хотите получать уведомления о варнах.")
        return

    await asyncio.to_thread(database.set_config, "warns_topic_id", str(thread_id))
    await update.message.reply_text(
        f"✅ Тема «ПРЕДЫ» установлена (ID: {thread_id}).\n\n"
        f"💡 <i>Примечание:</i> Для разделения топиков по дивизионам используйте команду /назначить_топик.",
        parse_mode="HTML"
    )


@admin_only
async def admin_fetch_photos(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /fetch_photos or admin_fetch_photos_cb — download and cache player portraits
    from hybrid free providers. Skips players that are already cached.
    """
    query = update.callback_query
    if query:
        await query.answer()

    force = False
    if context and context.args and any(a.lower() in ("force", "all", "refresh") for a in context.args):
        force = True
    elif query and "force" in str(query.data):
        force = True

    unique_players = await asyncio.to_thread(database.get_all_unique_players)

    if force:
        to_fetch = list(unique_players)
        already_cached = []
    else:
        already_cached = [item for item in unique_players if player_photos.is_cached(item[0], item[1])]
        to_fetch       = [item for item in unique_players if not player_photos.is_cached(item[0], item[1])]

    text_initial = (
        f"✅ Все {len(already_cached)} игроков уже имеют кэшированные фото."
        if not to_fetch else
        f"⏳ Загружаю фото для <b>{len(to_fetch)}</b> игроков (принудительно: {force})..."
    )

    if not to_fetch:
        if query:
            await query.edit_message_text(text_initial, parse_mode="HTML")
        elif update.message:
            await update.message.reply_text(text_initial, parse_mode="HTML")
        return

    if query:
        status_msg = await query.edit_message_text(text_initial, parse_mode="HTML")
    elif update.message:
        status_msg = await update.message.reply_text(text_initial, parse_mode="HTML")
    else:
        return

    ok_count   = 0
    fail_count = 0
    failed_names: list[str] = []

    for i, (name, team) in enumerate(to_fetch, 1):
        result = await asyncio.to_thread(player_photos.fetch_and_cache, name, team, force_refresh=force)

        if result:
            ok_count += 1
        else:
            fail_count += 1
            failed_names.append(f"{name} ({team})")

        # Update progress every 5 players
        if i % 5 == 0 or i == len(to_fetch):
            try:
                await status_msg.edit_text(
                    f"⏳ Прогресс: {i}/{len(to_fetch)} — "
                    f"✅ {ok_count} загружено, ❌ {fail_count} не найдено",
                    parse_mode="HTML"
                )
            except Exception:
                pass

    cleared_cache = 0
    if force:
        cleared_cache = await asyncio.to_thread(database.clear_telegram_media_cache)

    result_text = (
        f"✅ <b>Готово!</b>\n\n"
        f"Загружено: <b>{ok_count}</b>\n"
        f"Не найдено: <b>{fail_count}</b>\n"
        f"Уже были: <b>{len(already_cached)}</b>"
    )
    if cleared_cache > 0:
        result_text += f"\n🗑️ Очищен кэш карточек в базе: <b>{cleared_cache}</b> записей"
    if failed_names:
        sample = failed_names[:10]
        result_text += "\n\n<b>Не найдены:</b>\n" + "\n".join(f"• {html.escape(n)}" for n in sample)
        if len(failed_names) > 10:
            result_text += f"\n<i>...и ещё {len(failed_names) - 10}</i>"

    try:
        await status_msg.edit_text(result_text, parse_mode="HTML")
    except Exception:
        pass


# ===================== WARNS SYSTEM =====================

async def _send_to_warns_thread(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    division_id: int | None = None,
) -> None:
    """
    Send a message to the ПРЕДЫ thread of the division. Falls back silently.

    `division_id` обязателен по смыслу: у каждого дивизиона своя группа и свой
    тред ПРЕДЫ. Без него сообщение уйдёт в легаси-группу — это верно только для
    одногрупповых инсталляций.
    """
    group_id, topic_id = await resolve_division_target(
        division_id, "warns", "previews", legacy_topic_keys=("warns_topic_id",)
    )
    if not group_id:
        return
    kwargs = {"chat_id": group_id, "text": text, "parse_mode": "HTML"}
    if topic_id:
        kwargs["message_thread_id"] = int(topic_id)
    try:
        await context.bot.send_message(**kwargs)
    except Exception:
        logger.exception("Failed to send message to ПРЕДЫ thread")


async def _auto_kick_player(context: ContextTypes.DEFAULT_TYPE, user_id: int, username: str | None, team_name: str | None) -> None:
    """Ban player from league and soft-kick from group when warn limit exceeded."""
    # Дивизион снимаем ДО ban_and_remove_from_league: он обнуляет привязку игрока,
    # и после него кикать было бы уже неоткуда.
    kicked = await asyncio.to_thread(database.get_user, user_id)
    division_id = dict(kicked).get("division_id") if kicked else None

    await asyncio.to_thread(database.ban_and_remove_from_league, user_id)

    # Soft kick from Telegram group: ban and unban are handled separately so a
    # failed unban (which would leave a permanent ban instead of a kick) is logged.
    group_id, _ = await resolve_division_target(division_id)
    if group_id:
        try:
            await context.bot.ban_chat_member(chat_id=group_id, user_id=user_id)
        except (BadRequest, TelegramError) as e:
            logger.warning(f"Could not ban user {user_id} from group (DB ban already applied): {e}")
            return
        try:
            await context.bot.unban_chat_member(chat_id=group_id, user_id=user_id)
        except (BadRequest, TelegramError) as e:
            # User is now hard-banned instead of soft-kicked — surface loudly.
            logger.exception(f"User {user_id} banned but NOT unbanned after auto-kick: {e}")

    # DM to player
    uname = f"@{username}" if username else f"ID {user_id}"
    team_display = html.escape(team_name or "без клуба")
    dm_text = (
        f"⛔ <b>Вы исключены из турнира</b>\n\n"
        f"Вы набрали максимальное количество предупреждений: <b>{MAX_WARNS_LIMIT}/{MAX_WARNS_LIMIT}</b> за систематические долги по турам. "
        f"🏟 Клуб <b>{team_display}</b> освобождён и выставлен на замену.\n\n"
        f"<i>Для выяснения обстоятельств или апелляции обратитесь к администрации.</i>"
    )
    try:
        await context.bot.send_message(chat_id=user_id, text=dm_text, parse_mode="HTML")
    except (Forbidden, TelegramError):
        logger.warning(f"Cannot DM user {user_id} about auto-kick.")

    # Public notice in ПРЕДЫ thread & reports thread
    thread_text = (
        f"🚨 <b>АВТО-ИСКЛЮЧЕНИЕ УЧАСТНИКА</b>\n\n"
        f"👤 Игрок: <b>{html.escape(uname)}</b>\n"
        f"🏟 Клуб: <b>{team_display}</b>\n"
        f"Причина: Превышен лимит варнов ({MAX_WARNS_LIMIT}/{MAX_WARNS_LIMIT}) из-за несыгранных долгов.\n\n"
        f"📢 Клуб <b>{team_display}</b> свободен и открыт для замены!"
    )
    await _send_to_warns_thread(context, thread_text, division_id)

    # Also post to the division's reports topic if it has one
    rep_chat_id, rep_topic_id = await resolve_division_target(
        division_id, "reports", legacy_topic_keys=("reports_topic_id",)
    )
    if rep_chat_id and rep_topic_id:
        try:
            await context.bot.send_message(
                chat_id=rep_chat_id,
                text=thread_text,
                parse_mode="HTML",
                message_thread_id=int(rep_topic_id)
            )
        except Exception:
            pass

    try:
        await _post_or_update_debts_in_warns(context)
    except Exception as e:
        logger.warning(f"Failed to update debts in warns on auto-kick: {e}")


@admin_only
async def admin_warn_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show warn reason presets before issuing a warn."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    p_id = int(query.data.replace("warn_add_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)
    if not player:
        await query.edit_message_text("❌ Игрок не найден.")
        return

    warn_count = player['warn_count'] or 0
    username_str = f"@{player['username']}" if player['username'] else f"ID {p_id}"
    team_str = player['team_name'] or 'Без клуба'

    text = (
        f"⚠️ <b>Выдача предупреждения</b>\n\n"
        f"Игрок: <b>{html.escape(username_str)}</b> [{html.escape(team_str)}]\n"
        f"Текущий счётчик: <b>{warn_count} / {MAX_WARNS_LIMIT}</b>\n\n"
        f"Выберите причину:"
    )

    keyboard = []
    for idx, reason in enumerate(WARN_REASONS):
        keyboard.append([InlineKeyboardButton(reason, callback_data=f"warn_exec_{p_id}_{idx}")])
    keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data=f"admin_view_player_{p_id}")])

    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_warn_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Execute warn issuance with debounce protection."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    # Parse: warn_exec_{p_id}_{reason_idx}
    parts = query.data.replace("warn_exec_", "").rsplit("_", 1)
    if len(parts) != 2:
        return
    p_id = int(parts[0])
    reason_idx = int(parts[1])

    # Debounce
    if p_id in _warn_action_locks:
        await query.answer("⏳ Действие уже выполняется...", show_alert=True)
        return
    _warn_action_locks.add(p_id)

    try:
        player = await asyncio.to_thread(database.get_user, p_id)
        if not player:
            await query.edit_message_text("❌ Игрок не найден.")
            return
        # get_user отдаёт sqlite3.Row — у него нет .get().
        player = dict(player)

        reason = WARN_REASONS[reason_idx] if 0 <= reason_idx < len(WARN_REASONS) else WARN_REASONS[0]
        admin_id = query.from_user.id
        admin_username = query.from_user.username or str(admin_id)

        # A player without a club was already auto-kicked — issuing more warns
        # would trigger a duplicate kick and duplicate public announcements.
        if not player.get('team_name'):
            await query.answer(
                "⛔ Игрок без клуба уже исключён из лиги. Выдача варнов заблокирована.",
                show_alert=True
            )
            return

        new_count, is_exceeded = await asyncio.to_thread(database.add_warn, p_id, admin_id, reason)

        username_str = f"@{player['username']}" if player['username'] else f"ID {p_id}"
        team_str = player['team_name'] or 'Без клуба'

        if is_exceeded:
            # Auto-kick
            await _auto_kick_player(context, p_id, player['username'], player['team_name'])
            result_text = (
                f"🚨 Игрок <b>{html.escape(username_str)}</b> [{html.escape(team_str)}] получил "
                f"<b>{new_count}/{MAX_WARNS_LIMIT}</b> предупреждений!\n\n"
                f"⛔ Автоматически исключен из лиги и группы. Клуб освобожден."
            )
        else:
            # DM to player
            dm_text = (
                f"⚠️ <b>Вам выдано предупреждение!</b>\n\n"
                f"Причина: {html.escape(reason)}\n"
                f"Счётчик: <b>{new_count} / {MAX_WARNS_LIMIT}</b>\n\n"
                f"Администратор: @{html.escape(admin_username)}"
            )
            try:
                await context.bot.send_message(chat_id=p_id, text=dm_text, parse_mode="HTML")
            except (Forbidden, TelegramError):
                logger.warning(f"Cannot DM user {p_id} about warn.")

            # Thread notification
            thread_text = (
                f"⚠️ Игроку <b>{html.escape(username_str)}</b> [{html.escape(team_str)}] "
                f"выдан варн (<b>{new_count}/{MAX_WARNS_LIMIT}</b>).\n"
                f"Причина: {html.escape(reason)}\n"
                f"Администратор: @{html.escape(admin_username)}"
            )
            await _send_to_warns_thread(context, thread_text, player.get("division_id"))

            result_text = (
                f"✅ Варн выдан игроку <b>{html.escape(username_str)}</b>.\n"
                f"Счётчик: <b>{new_count} / {MAX_WARNS_LIMIT}</b>"
            )

        keyboard = [[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{p_id}")]]
        await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        await _post_or_update_debts_in_warns(context)

    finally:
        _warn_action_locks.discard(p_id)


@admin_only
async def admin_warn_remove_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a warn from a player."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    p_id = int(query.data.replace("warn_remove_", ""))

    # Debounce
    if p_id in _warn_action_locks:
        await query.answer("⏳ Действие уже выполняется...", show_alert=True)
        return
    _warn_action_locks.add(p_id)

    try:
        player = await asyncio.to_thread(database.get_user, p_id)
        if not player:
            await query.edit_message_text("❌ Игрок не найден.")
            return
        player = dict(player)

        warn_count = player['warn_count'] or 0
        if warn_count <= 0:
            await query.answer("У игрока нет активных предупреждений.", show_alert=True)
            _warn_action_locks.discard(p_id)
            return

        admin_id = query.from_user.id
        admin_username = query.from_user.username or str(admin_id)
        reason = "Снятие варна администратором"

        new_count, success = await asyncio.to_thread(database.remove_warn, p_id, admin_id, reason)

        if not success:
            await query.answer("У игрока нет активных предупреждений.", show_alert=True)
            _warn_action_locks.discard(p_id)
            return

        username_str = f"@{player['username']}" if player['username'] else f"ID {p_id}"
        team_str = player['team_name'] or 'Без клуба'

        # DM to player
        dm_text = (
            f"🟢 <b>Предупреждение снято!</b>\n\n"
            f"Ваш счётчик: <b>{new_count} / {MAX_WARNS_LIMIT}</b>\n"
            f"Администратор: @{html.escape(admin_username)}"
        )
        try:
            await context.bot.send_message(chat_id=p_id, text=dm_text, parse_mode="HTML")
        except (Forbidden, TelegramError):
            pass

        # Thread notification
        thread_text = (
            f"🟢 Игроку <b>{html.escape(username_str)}</b> [{html.escape(team_str)}] "
            f"снят варн (<b>{new_count}/{MAX_WARNS_LIMIT}</b>).\n"
            f"Администратор: @{html.escape(admin_username)}"
        )
        await _send_to_warns_thread(context, thread_text, player.get("division_id"))

        result_text = f"✅ Варн снят. Счётчик: <b>{new_count} / {MAX_WARNS_LIMIT}</b>"
        keyboard = [[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{p_id}")]]
        await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        await _post_or_update_debts_in_warns(context)

    finally:
        _warn_action_locks.discard(p_id)


@admin_only
async def admin_warn_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show warn history for a player."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    p_id = int(query.data.replace("warn_hist_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)
    if not player:
        await query.edit_message_text("❌ Игрок не найден.")
        return

    warns = await asyncio.to_thread(database.get_user_warns, p_id)
    username_str = f"@{player['username']}" if player['username'] else f"ID {p_id}"
    warn_count = player['warn_count'] or 0

    text = (
        f"📜 <b>История варнов</b>\n"
        f"Игрок: <b>{html.escape(username_str)}</b>\n"
        f"Текущий счётчик: <b>{warn_count} / {MAX_WARNS_LIMIT}</b>\n\n"
    )

    if not warns:
        text += "<i>История пуста.</i>"
    else:
        for w in warns[:20]:
            w_type = w['type']
            if w_type == 'WARN_ADD':
                icon = "⚠️"
            elif w_type == 'WARN_REMOVE':
                icon = "🟢"
            elif w_type == 'AUTO_KICK':
                icon = "🚨"
            else:
                icon = "❓"

            date_str = str(w['created_at'])[:16] if w['created_at'] else "?"
            reason_str = html.escape(w['reason'] or '-')
            text += f"{icon} <code>{date_str}</code> — {reason_str}\n"

    keyboard = [[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{p_id}")]]
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))


@admin_only
async def admin_amnesty_execute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all warns for a player (amnesty)."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    p_id = int(query.data.replace("warn_amnesty_", ""))
    player = await asyncio.to_thread(database.get_user, p_id)
    if not player:
        await query.edit_message_text("❌ Игрок не найден.")
        return
    player = dict(player)

    admin_id = query.from_user.id
    await asyncio.to_thread(database.amnesty_player, p_id, admin_id)

    username_str = f"@{player['username']}" if player['username'] else f"ID {p_id}"
    team_str = player['team_name'] or 'Без клуба'

    # DM
    try:
        await context.bot.send_message(
            chat_id=p_id,
            text="🕊 <b>Амнистия!</b>\n\nВаши предупреждения сброшены до 0.",
            parse_mode="HTML"
        )
    except (Forbidden, TelegramError):
        pass

    # Thread
    thread_text = (
        f"🕊 Игроку <b>{html.escape(username_str)}</b> [{html.escape(team_str)}] "
        f"применена амнистия. Счётчик варнов сброшен до 0.\n"
        f"Администратор: @{html.escape(query.from_user.username or str(admin_id))}"
    )
    await _send_to_warns_thread(context, thread_text, player.get("division_id"))

    result_text = f"✅ Амнистия применена к <b>{html.escape(username_str)}</b>. Счётчик: <b>0 / {MAX_WARNS_LIMIT}</b>"
    keyboard = [[InlineKeyboardButton("« К карточке игрока", callback_data=f"admin_view_player_{p_id}")]]
    await query.edit_message_text(result_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
    await _post_or_update_debts_in_warns(context)


@admin_only
async def admin_reset_season_warns(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reset all warns for all players (new season)."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer()
    except BadRequest:
        pass

    await asyncio.to_thread(database.reset_season_warns)
    await query.edit_message_text(
        "✅ Все предупреждения сброшены (новый сезон).",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Назад в админку", callback_data="admin_main_menu")]])
    )
    await _post_or_update_debts_in_warns(context)


@admin_only
async def admin_reset_debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command /reset_debts or /reset_warns: resets all warns, clears debt timers, and confirms start datetime."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return

    count = await asyncio.to_thread(database.admin_reset_all_warns_and_debts)

    text = (
        f"🧹 <b>Система долгов и варнов успешно сброшена!</b>\n\n"
        f"• Сброшено варнов у игроков: <b>{count}</b>\n"
        f"• Все таймеры и стадии долгов очищены.\n\n"
        f"<i>Отсчёт долгов идёт от дедлайна тура: пока дедлайн не истёк, "
        f"авто-варны не выписываются.</i>"
    )
    await update.message.reply_text(text, parse_mode="HTML")
    await _post_or_update_debts_in_warns(context)


@admin_only
async def admin_unwarn_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin command /unwarn <@user / team>: removes 1 warn from user, or /unwarn all <@user> resets to 0."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("❌ Нет прав.")
        return

    args = context.args or []
    if not args:
        await update.message.reply_text(
            "ℹ️ <b>Использование:</b>\n"
            "• <code>/unwarn @username</code> — снять 1 варн с игрока\n"
            "• <code>/unwarn all @username</code> — полностью обнулить варны игрока\n"
            "• <code>/reset_debts</code> — сбросить все варны и стадии долгов всей лиги",
            parse_mode="HTML"
        )
        return

    is_all = (args[0].lower() == "all" and len(args) > 1)
    target_ref = args[1] if is_all else args[0]
    target_ref = target_ref.lstrip("@").strip()

    target_user = await asyncio.to_thread(database.find_user_by_ref, target_ref)
    if not target_user:
        await update.message.reply_text(f"❌ Пользователь <b>{html.escape(target_ref)}</b> не найден.", parse_mode="HTML")
        return

    target_user = dict(target_user)
    t_id = target_user["telegram_id"]
    u_name = f"@{target_user.get('username')}" if target_user.get('username') else f"ID {t_id}"
    t_name = target_user.get('team_name') or "без клуба"

    if is_all:
        await asyncio.to_thread(database.reset_user_warns, t_id, user_id)
        await update.message.reply_text(
            f"✅ Все варны игрока <b>{html.escape(u_name)}</b> [{html.escape(t_name)}] полностью аннулированы (0/{MAX_WARNS_LIMIT}).",
            parse_mode="HTML"
        )
    else:
        new_cnt, removed = await asyncio.to_thread(database.remove_warn, t_id, user_id, "Снято администратором")
        if removed:
            await update.message.reply_text(
                f"✅ С игрока <b>{html.escape(u_name)}</b> [{html.escape(t_name)}] снят 1 варн.\n"
                f"📊 Текущие варны: <b>{new_cnt}/{MAX_WARNS_LIMIT}</b>",
                parse_mode="HTML"
            )
        else:
            await update.message.reply_text(
                f"ℹ️ У игрока <b>{html.escape(u_name)}</b> [{html.escape(t_name)}] нет активных варнов (0/{MAX_WARNS_LIMIT}).",
                parse_mode="HTML"
            )

    await _post_or_update_debts_in_warns(context)


@admin_only
async def admin_ai_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate AI summary for the current tournament round."""
    query = update.callback_query
    if not query:
        return
    try:
        await query.answer("Генерируем итоги... Это может занять несколько секунд.", show_alert=True)
    except BadRequest:
        pass

    # Fetch standings
    standings = await asyncio.to_thread(database.get_standings)
    top_scorers = await asyncio.to_thread(database.get_top_scorers, 1)
    top_assists = await asyncio.to_thread(database.get_top_assists, 1)

    top_scorer = top_scorers[0] if top_scorers else None
    top_assist = top_assists[0] if top_assists else None

    # Call AI
    from services.ai.ai_chat import generate_tournament_summary
    summary = await asyncio.to_thread(generate_tournament_summary, standings, top_scorer, top_assist)
    summary = html.escape(summary)

    # Send to admin in DM
    user_id = query.from_user.id
    try:
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🤖 <b>Сгенерированные итоги круга (AI):</b>\n\n{summary}\n\n<i>Скопируйте этот текст и отправьте в нужный чат/канал!</i>",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to send AI summary to admin {user_id}: {e}")
        if query.message:
            await query.message.reply_text("❌ Ошибка при отправке итогов в ЛС. Проверьте, что бот может писать вам сообщения.")