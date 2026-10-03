"""services/irl_jobs.py — фоновые джобы ставок на реальные матчи.

Три джобы, каждая — тонкая обёртка над `run_*`, которую можно гонять в тестах с
поддельными провайдером и ботом:

* `irl_pick`          — раз в день выбирает топ-матч(и), шлёт админам превью и
                        публикует матч (если `IRL_AUTO_PUBLISH`);
* `irl_odds_refresh`  — подтягивает кэфы выбранного букмекера для черновиков и
                        открытых матчей;
* `irl_settle`        — закрывает начавшиеся матчи и считает их по счёту основного
                        времени от провайдера; сомнительное отдаёт админам.

Различие провайдера «упал» (`None`) и «пусто» (`[]`) соблюдается везде: сбой не
превращается ни в объявление «матчей нет», ни в расчёт.
"""

import dataclasses
import html
import logging
from datetime import datetime, timedelta
from typing import Optional

import config
import database
from services import irl_betting
from time_utils import MSK_LABEL, fmt_msk, now_msk, parse_msk

logger = logging.getLogger(__name__)

# Матч публикуется не позже, чем за столько минут до начала.
PUBLISH_BEFORE_KICKOFF = timedelta(minutes=30)
# Пауза между повторами выбора, когда день пока пуст или провайдер недоступен.
PICK_RETRY = timedelta(minutes=30)
# Кэфы обновляются только у матчей, которые начнутся в ближайшие сутки.
ODDS_HORIZON = timedelta(hours=24)

_next_pick_try: dict[str, datetime] = {}


def reset_state() -> None:
    """Сбросить память джоб (для тестов)."""
    _next_pick_try.clear()


# ─── Админские сообщения ─────────────────────────────────────────────────────

async def notify_admins(bot, text: str, reply_markup=None) -> int:
    """Личка глобальным админам; недоставка одному не мешает остальным. → число доставленных."""
    sent = 0
    for admin_id in list(config.ADMIN_IDS):
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML",
                                   reply_markup=reply_markup)
            sent += 1
        except Exception as e:
            logger.warning("IRL notice to %s failed: %s", admin_id, e)
    return sent


async def _notify_once(bot, key: str, text: str, reply_markup=None) -> bool:
    """Одноразовое уведомление: ключ ставится только после доставки хотя бы одному админу."""
    if database.irl_notice_sent(key):
        return False
    if await notify_admins(bot, text, reply_markup) == 0:
        return False
    database.mark_irl_notice(key)
    return True


def _odds_line(m: dict) -> str:
    return f"П1 {m['odd_home']:.2f} · Х {m['odd_draw']:.2f} · П2 {m['odd_away']:.2f}"


def _match_line(m: dict) -> str:
    league = f"{html.escape(str(m['league_name']))}\n" if m.get("league_name") else ""
    return (f"<b>#{m['id']}</b> {league}"
            f"{html.escape(str(m['home']))} — {html.escape(str(m['away']))}\n"
            f"🕒 {fmt_msk(m['kickoff_at'], '%d.%m %H:%M')} {MSK_LABEL} · {_odds_line(m)}")


def build_preview(matches: list[dict], auto_publish: Optional[bool] = None) -> str:
    """Текст превью дня для админов."""
    auto = config.IRL_AUTO_PUBLISH if auto_publish is None else auto_publish
    head = "⚽ <b>IRL-ставки: матчи дня</b>"
    body = "\n\n".join(_match_line(m) for m in matches)
    if auto:
        tail = (f"Публикация автоматически не позже {config.IRL_AUTO_PUBLISH_HOUR_MSK:02d}:00 {MSK_LABEL} "
                f"(и не позже чем за {int(PUBLISH_BEFORE_KICKOFF.total_seconds() // 60)} мин до начала).")
    else:
        tail = "Автопубликация выключена — матч не откроется, пока вы его не опубликуете."
    return f"{head}\n\n{body}\n\n{tail}"


# ─── Выбор матча дня ─────────────────────────────────────────────────────────

def _rank_map(standings) -> dict[str, int]:
    """Строки таблицы провайдера → {нормализованное имя команды: место}. Мусор пропускается."""
    ranks: dict[str, int] = {}
    for row in standings or []:
        try:
            rank = int(row["rank"])
            name = irl_betting.normalize_name(row["team"]["name"])
        except (KeyError, TypeError, ValueError):
            continue
        if name and rank > 0:
            ranks[name] = rank
    return ranks


async def _collect_candidates(provider, fixtures, bookmaker_id: int, priority: list[int]):
    """Кэфы и места в таблице для матчей приоритетных лиг.

    Запросы идут по лигам в порядке приоритета и прекращаются на первой лиге с
    годным кандидатом: ниже по списку матч всё равно не выберется, а квота запросов
    ограничена. → `(candidates, odds_failed, higher_missing)`, где `odds_failed` —
    ответ по кэфам не получен, `higher_missing` — в более приоритетной лиге есть
    матч без кэфов (возможно, букмекер ещё не выставил линию).
    """
    candidates: list[irl_betting.Candidate] = []
    odds_failed = 0
    higher_missing = False
    for league_id in priority:
        league_fx = [f for f in fixtures if f.league_id == league_id]
        if not league_fx:
            continue
        found: list[irl_betting.Candidate] = []
        missing = False
        for fx in league_fx:
            odds = await provider.get_match_winner_odds(fx.fixture_id, bookmaker_id)
            if odds is None:
                odds_failed += 1
                missing = True
                continue
            found.append(irl_betting.Candidate(
                fixture_id=fx.fixture_id, league_id=fx.league_id, league_name=fx.league_name,
                home=fx.home, away=fx.away, kickoff=fx.kickoff,
                odd_home=odds.home, odd_draw=odds.draw, odd_away=odds.away,
            ))
        if found:
            season = next((f.season for f in league_fx if f.season), None)
            if season:
                ranks = _rank_map(await provider.get_standings(league_id, season))
                found = [
                    dataclasses.replace(c, home_rank=ranks.get(irl_betting.normalize_name(c.home)),
                                        away_rank=ranks.get(irl_betting.normalize_name(c.away)))
                    for c in found
                ]
            candidates.extend(found)
            return candidates, odds_failed, higher_missing
        higher_missing = higher_missing or missing
    return candidates, odds_failed, higher_missing


async def run_pick(bot, provider=None, now: Optional[datetime] = None) -> dict:
    """Один проход выбора: истечение черновиков, выбор дня, публикация. → краткий отчёт."""
    report = {"picked": 0, "published": 0, "expired": 0, "note": ""}
    if not config.IRL_ENABLED:
        report["note"] = "disabled"
        return report
    now = now or now_msk()
    day = now.date().isoformat()

    report["expired"] = database.expire_irl_drafts()
    database.close_started_irl_matches()

    if not database.list_irl_matches(bet_day=day) and now.hour >= config.IRL_PREVIEW_HOUR_MSK:
        await _pick_for_day(bot, provider, now, day, report)
    await _send_preview(bot, day)

    report["published"] = _publish_due(now, day)
    return report


async def _pick_for_day(bot, provider, now: datetime, day: str, report: dict) -> None:
    if not config.IRL_BOOKMAKER_ID:
        await _notify_once(bot, f"nobookmaker:{day}",
                           "⚠️ IRL-ставки включены, но не задан <code>IRL_BOOKMAKER_ID</code> — "
                           "матч дня не выбран.")
        report["note"] = "no_bookmaker"
        return
    retry_at = _next_pick_try.get(day)
    if retry_at and now < retry_at:
        report["note"] = "throttled"
        return
    _next_pick_try[day] = now + PICK_RETRY

    if provider is None:
        from services.sports import get_sports_provider
        provider = get_sports_provider()

    priority = list(config.IRL_COMPETITION_PRIORITY)
    fixtures = await provider.get_prematch_fixtures(day, priority)
    if fixtures is None:
        await _notify_once(bot, f"provider_down:{day}",
                           "⚠️ IRL-ставки: провайдер не отдал расписание матчей. Повторю позже.")
        report["note"] = "provider_down"
        return
    upcoming = [f for f in fixtures if f.kickoff > now]
    # Матчи, уже занесённые в базу (например, отменённые админом), заново не выбираются.
    upcoming = [f for f in upcoming if not database.get_irl_match_by_fixture(f.fixture_id)]

    candidates, odds_failed, higher_missing = await _collect_candidates(
        provider, upcoming, int(config.IRL_BOOKMAKER_ID), priority)
    publish_hour_passed = now.hour >= config.IRL_AUTO_PUBLISH_HOUR_MSK
    if candidates and higher_missing and not publish_hour_passed:
        # Букмекер мог ещё не выставить линию на более важный матч — подождём до часа публикации.
        report["note"] = "waiting_for_higher_odds"
        return

    picked = irl_betting.pick_top_matches(candidates, now, priority=priority)
    if not picked:
        if not upcoming:
            await _notify_once(bot, f"empty:{day}",
                               "ℹ️ IRL-ставки: сегодня нет подходящих матчей из списка турниров — "
                               "ставки не открываются.")
            report["note"] = "no_matches"
        elif odds_failed and not publish_hour_passed:
            report["note"] = "odds_unavailable"
        else:
            await _notify_once(bot, f"empty:{day}",
                               "ℹ️ IRL-ставки: на сегодняшние матчи нет коэффициентов выбранного "
                               "букмекера — ставки не открываются.")
            report["note"] = "no_odds"
        return

    created = []
    for c in picked:
        match_id, is_new = database.create_irl_draft(
            c.fixture_id, c.league_id, c.league_name, c.home, c.away, c.kickoff,
            c.odd_home, c.odd_draw, c.odd_away, bet_day=c.kickoff.date().isoformat(), picked_by="auto")
        if is_new:
            created.append(match_id)
    report["picked"] = len(created)
    _next_pick_try.pop(day, None)


async def _send_preview(bot, day: str) -> None:
    """Превью дня — пока его не получит хотя бы один админ (переживает сбой доставки и рестарт)."""
    if database.irl_notice_sent(f"preview:{day}"):
        return
    matches = database.list_irl_matches(bet_day=day, statuses=("draft", "open"))
    if matches:
        from handlers.admin_irl import day_keyboard
        await _notify_once(bot, f"preview:{day}", build_preview(matches), day_keyboard(matches, day))


def _publish_due(now: datetime, day: str) -> int:
    """Публикует черновики дня, у которых наступил срок (час публикации или за 30 мин до начала)."""
    if not config.IRL_AUTO_PUBLISH:
        return 0
    published = 0
    publish_at = datetime.combine(now.date(), datetime.min.time()) + timedelta(
        hours=config.IRL_AUTO_PUBLISH_HOUR_MSK)
    for m in database.list_irl_matches(bet_day=day, statuses=("draft",)):
        kickoff = parse_msk(m["kickoff_at"])
        if kickoff is None:
            continue
        if now >= min(publish_at, kickoff - PUBLISH_BEFORE_KICKOFF):
            ok, message = database.publish_irl_match(m["id"])
            if ok:
                published += 1
            else:
                logger.warning("IRL auto-publish of #%s refused: %s", m["id"], message)
    return published


# ─── Кэфы ────────────────────────────────────────────────────────────────────

async def run_odds_refresh(provider=None, now: Optional[datetime] = None) -> int:
    """Обновляет кэфы черновиков и открытых матчей ближайших суток. → число обновлённых."""
    if not config.IRL_ENABLED or not config.IRL_BOOKMAKER_ID:
        return 0
    now = now or now_msk()
    if provider is None:
        from services.sports import get_sports_provider
        provider = get_sports_provider()
    updated = 0
    for m in database.list_irl_matches(statuses=("draft", "open")):
        kickoff = parse_msk(m["kickoff_at"])
        if kickoff is None or kickoff <= now or kickoff - now > ODDS_HORIZON:
            continue
        odds = await provider.get_match_winner_odds(m["provider_fixture_id"], int(config.IRL_BOOKMAKER_ID))
        if odds is None:
            continue    # сбой или неполный рынок — остаются прежние цены
        if (odds.home, odds.draw, odds.away) == (m["odd_home"], m["odd_draw"], m["odd_away"]):
            continue
        if database.update_irl_odds(m["id"], odds.home, odds.draw, odds.away):
            updated += 1
    return updated


# ─── Расчёт ──────────────────────────────────────────────────────────────────

_RESULT_LABEL = {"home": "П1", "draw": "Х", "away": "П2"}


def _manual_text(m: dict, reason: str) -> str:
    stats = database.get_irl_match_bet_stats(m["id"])
    return (f"🛠 <b>IRL-матч #{m['id']} требует ручного расчёта</b>\n"
            f"{html.escape(str(m['home']))} — {html.escape(str(m['away']))}, "
            f"начало {fmt_msk(m['kickoff_at'], '%d.%m %H:%M')} {MSK_LABEL}\n"
            f"{html.escape(reason)}\n"
            f"Ставок: {stats['count']} на {stats['total']:,} 🪙\n\n"
            f"<code>/irl_settle {m['id']} 1|X|2|void</code>")


async def run_settle(bot, provider=None, now: Optional[datetime] = None) -> dict:
    """Закрывает начавшиеся матчи и считает завершённые. → `{"settled", "voided", "manual"}`."""
    report = {"settled": 0, "voided": 0, "manual": 0}
    if not config.IRL_ENABLED:
        return report
    now = now or now_msk()
    database.close_started_irl_matches()
    if provider is None:
        from services.sports import get_sports_provider
        provider = get_sports_provider()

    for m in database.list_irl_matches(statuses=("open", "closed"), limit=200):
        kickoff = parse_msk(m["kickoff_at"])
        if kickoff is None or kickoff > now:
            continue
        fx = await provider.get_prematch_fixture(m["provider_fixture_id"])
        if fx is None:
            decision = irl_betting.SettleDecision("wait", reason="Провайдер недоступен")
        else:
            decision = irl_betting.settle_decision(
                fx.status_short, fx.home_goals, fx.away_goals, kickoff, now)

        if decision.action == "result":
            stats = database.get_irl_match_bet_stats(m["id"])
            ok, info = database.settle_irl_match(m["id"], decision.result, fx.home_goals, fx.away_goals)
            if ok:
                report["settled"] += 1
                await notify_admins(bot, (
                    f"✅ IRL-матч #{m['id']} рассчитан: {html.escape(str(m['home']))} "
                    f"{fx.home_goals}:{fx.away_goals} {html.escape(str(m['away']))} → "
                    f"<b>{_RESULT_LABEL[decision.result]}</b>\n"
                    f"Ставок: {stats['count']}, выиграло {info['won']}, выплачено {info['paid']:,} 🪙"))
            else:
                logger.warning("IRL settle #%s refused: %s", m["id"], info)
        elif decision.action == "void":
            ok, info = database.void_irl_match(m["id"], decision.reason)
            if ok:
                report["voided"] += 1
                await notify_admins(bot, (
                    f"↩️ IRL-матч #{m['id']} аннулирован: {html.escape(decision.reason)}. "
                    f"Возвращено ставок: {info['refunded']}."))
        elif irl_betting.needs_manual_settlement(kickoff, now):
            if await _notify_once(bot, f"manual:{m['id']}", _manual_text(m, decision.reason)):
                report["manual"] += 1
    return report


# ─── Обёртки для job_queue ───────────────────────────────────────────────────

async def job_irl_pick(context) -> None:
    await run_pick(context.bot)


async def job_irl_odds_refresh(context) -> None:
    await run_odds_refresh()


async def job_irl_settle(context) -> None:
    await run_settle(context.bot)
