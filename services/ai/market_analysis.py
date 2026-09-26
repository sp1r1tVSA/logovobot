"""
services/ai/market_analysis.py

Вкладка «Анализ рынка» в панели Logovo.bet: где игроки обыгрывают линию,
откуда в экономику текут монеты и какие лимиты стоит ужесточить.

Как считается:
  1. Статистика — детерминированно, из database.get_market_analysis_data:
     купоны, рассчитанные за период (7 / 14 / 30 дней, MSK), и все открытые.
     Разрезы — по дивизионам (кубок дивизиона — к своему дивизиону, общий
     кубок отдельно), по группам ставок (группы запретов, экспресс отдельно),
     по игрокам; плюс движение монет по типам операций.
     Экспресс с ногами из нескольких дивизионов учитывается в каждом из них,
     как на сводке панели, — в итог «все дивизионы» он входит один раз.
  2. Правила (rule_suggestions) из тех же цифр предлагают ужесточения:
     запрет убыточной группы в дивизионе, потолок выигрыша и ставки, число
     открытых купонов, надбавку на экспресс, личный потолок крупнейшему
     победителю.
  3. Модель OpenRouter (та же цепочка, что у «ИИ-прогноза») получает цифры,
     текущие лимиты, допустимые ключи и предложения правил, а возвращает
     отчёт и свой список предложений. Каждое её предложение проверяется как
     ввод админа: уровень, ключ, диапазон, существующий дивизион или игрок,
     и только ужесточение. Негодное отбрасывается.
  4. Нет ключа, модель не ответила или не дала ни одного годного
     предложения — показываются предложения правил (suggestions_source="rules").

Анализ ничего не меняет сам: админ ставит лимит кнопкой «Установить», и она
идёт через обычный POST /api/admin/panel/limits с его проверками и журналом.
"""
from __future__ import annotations

import datetime
import json
import logging
import math
import threading
import time

import config
import database
from services.ai import bet_picks
from services.betting_limits import (
    BAN_SCOPES, DEFAULT_LIMIT_BOUNDS, LIMIT_BOUNDS, LIMIT_KEYS_BY_SCOPE, BettingLimitsService,
)
from time_utils import now_msk, now_msk_str

logger = logging.getLogger(__name__)

PERIODS = (7, 14, 30)
DEFAULT_PERIOD = 14
# Меньше рассчитанного оборота — выводов по дивизиону или группе не делаем:
# пара удачных купонов даёт маржу −700%, и это шум, а не проблема линии.
MIN_SCOPE_STAKE = 5_000
MIN_GROUP_STAKE = 2_000
LOSS_MARGIN = -10.0        # маржа букмекера ниже — дивизион в минусе
HIGH_LOSS_MARGIN = -25.0   # ниже — ужесточать сильнее
BAN_MARGIN = -50.0         # группа ставок с такой маржой — кандидат на запрет
MIN_SUGGESTED_MAX_BET = 1_000
WINNER_MIN_PROFIT = 5_000
WINNER_MIN_SHARE = 30.0    # % всей прибыли игроков у одного человека
MAX_SUGGESTIONS = 12
TOP_WINNERS = 5
SUMMARY_MAX_CHARS = 1_500
REASON_MAX_CHARS = 300
RISK_MAX_CHARS = 300
MAX_RISKS = 8

CACHE_TTL_SECONDS = bet_picks.CACHE_TTL_SECONDS
FALLBACK_TTL_SECONDS = bet_picks.FALLBACK_TTL_SECONDS
REFRESH_MIN_SECONDS = bet_picks.REFRESH_MIN_SECONDS

_cache: dict[int, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_call_lock = threading.Lock()

SETTLED = ("won", "lost", "cashed_out")
PAID = ("won", "cashed_out")
VOID = ("refunded", "cancelled")
GENERAL_CUP_LABEL = "Общий кубок"
ALL_DIVISIONS_LABEL = "Все дивизионы"

GROUP_LABELS = {g: label for g, (label, _keys) in database.BET_BAN_GROUPS.items()}
GROUP_LABELS["other"] = "Прочее"

# Строже — больше; у остальных ключей строже — меньше.
STRICTER_WHEN_HIGHER = {"min_bet", "express_margin_pct"}

LIMIT_LABELS = {
    "min_bet": "Минимальная ставка",
    "max_bet": "Максимальная ставка",
    "max_payout": "Максимальный выигрыш",
    "max_daily_stake": "Ставок за день",
    "max_daily_loss": "Проигрыш за день",
    "max_open_exposure": "Сумма в открытых купонах",
    "max_open_bets": "Открытых купонов",
    "market_exposure_limit": "Риск на рынок",
    "division_exposure_limit": "Риск на дивизион",
    "global_exposure_limit": "Риск на все дивизионы",
    "max_express_events": "Событий в экспрессе",
    "express_margin_pct": "Надбавка на экспресс, %",
    "initial_balance": "Стартовый баланс",
}
# Что значит ключ — для модели: она выбирает, что ужесточать.
LIMIT_MEANINGS = {
    "min_bet": "минимальная ставка одного купона",
    "max_bet": "максимальная ставка одного купона",
    "max_payout": "максимальный выигрыш одного купона",
    "max_daily_stake": "сколько игрок может поставить за сутки",
    "max_daily_loss": "сколько игрок может проиграть за сутки",
    "max_open_exposure": "сумма ставок игрока в нерассчитанных купонах",
    "max_open_bets": "сколько купонов игрок держит открытыми одновременно",
    "market_exposure_limit": "риск букмекера на один рынок",
    "division_exposure_limit": "риск букмекера на дивизион",
    "global_exposure_limit": "риск букмекера на все дивизионы",
    "max_express_events": "сколько событий можно собрать в экспресс",
    "express_margin_pct": "на сколько % режется коэффициент экспресса за каждое событие после первого",
    "initial_balance": "стартовый баланс нового кошелька",
}

FLOW_LABELS = {
    "bet_placed": "Ставки",
    "bet_won": "Выигрыши",
    "cashout": "Кэшаут",
    "bet_refund": "Возвраты ставок",
    "void_refund": "Возвраты ставок",
    "market_void_bet_refund": "Возвраты ставок",
    "resettle_refund": "Перерасчёт",
    "achievement_reward": "Достижения",
    "level_up_reward": "Новый уровень",
    "welcome_bonus": "Приветственный бонус",
    "daily_bonus": "Ежедневный бонус",
    "season_reward": "Награды сезона",
    "admin_credit": "Начисления админа",
    "admin_debit": "Списания админа",
    "admin_refund": "Возвраты админа",
    "balance_reset": "Сброс баланса",
}
BET_FLOWS = {"bet_placed", "bet_won", "cashout", "bet_refund", "void_refund",
             "market_void_bet_refund", "resettle_refund"}
REWARD_FLOWS = {"achievement_reward", "level_up_reward", "welcome_bonus", "daily_bonus", "season_reward"}


def normalize_period(value) -> int:
    """Период анализа в днях; ValueError — на всё, кроме PERIODS."""
    if value is None or str(value).strip() == "":
        return DEFAULT_PERIOD
    days = int(str(value).strip())
    if days not in PERIODS:
        raise ValueError("unsupported period")
    return days


def fmt_coins(value: float | int) -> str:
    return f"{int(round(value)):,}".replace(",", " ")


def _margin(ggr: float, stake: float) -> float | None:
    return round(ggr * 100.0 / stake, 1) if stake else None


# ─── Статистика ─────────────────────────────────────────────────────────────

def _bucket() -> dict:
    return {"stake": 0, "paid": 0, "settled_bets": 0, "turnover": 0, "created_bets": 0,
            "pending_count": 0, "pending_stake": 0, "pending_liability": 0,
            "bettors": set(), "stakes": [], "groups": {}}


def _add(bucket: dict, bet: dict, group: str, settled: bool, created: bool) -> None:
    status = bet["status"]
    amount = int(bet["amount"] or 0)
    if settled:
        paid = int(bet["actual_payout"] or 0) if status in PAID else 0
        bucket["stake"] += amount
        bucket["paid"] += paid
        bucket["settled_bets"] += 1
        g = bucket["groups"].setdefault(group, {"stake": 0, "paid": 0, "bets": 0})
        g["stake"] += amount
        g["paid"] += paid
        g["bets"] += 1
    if status == "pending":
        bucket["pending_count"] += 1
        bucket["pending_stake"] += amount
        bucket["pending_liability"] += int(bet["potential_win"] or 0)
    if created:
        bucket["turnover"] += amount
        bucket["created_bets"] += 1
        bucket["bettors"].add(bet["user_id"])
        bucket["stakes"].append(amount)


def _finish(bucket: dict) -> dict:
    ggr = bucket["stake"] - bucket["paid"]
    groups = []
    for gid, g in bucket["groups"].items():
        g_ggr = g["stake"] - g["paid"]
        groups.append({"group": gid, "label": GROUP_LABELS.get(gid, gid), "stake": g["stake"],
                       "paid": g["paid"], "ggr": g_ggr, "margin_pct": _margin(g_ggr, g["stake"]),
                       "bets": g["bets"]})
    groups.sort(key=lambda g: g["ggr"])
    return {
        "settled_stake": bucket["stake"],
        "paid_out": bucket["paid"],
        "ggr": ggr,
        "margin_pct": _margin(ggr, bucket["stake"]),
        "settled_bets": bucket["settled_bets"],
        "turnover": bucket["turnover"],
        "bets": bucket["created_bets"],
        "bettors": len(bucket["bettors"]),
        "pending_count": bucket["pending_count"],
        "pending_stake": bucket["pending_stake"],
        "pending_liability": bucket["pending_liability"],
        "stake_p90": _percentile(bucket["stakes"], 0.90),
        "stake_p95": _percentile(bucket["stakes"], 0.95),
        "groups": groups,
    }


def _percentile(values: list[int], q: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))
    return ordered[idx]


def compute_stats(data: dict, days: int, since: str) -> dict:
    legs_by_bet: dict[int, list[dict]] = {}
    for leg in data["legs"]:
        legs_by_bet.setdefault(leg["bet_id"], []).append(leg)

    total = _bucket()
    scopes: dict[int | None, dict] = {}
    users: dict[int, dict] = {}
    for bet in data["bets"]:
        legs = legs_by_bet.get(bet["id"], [])
        if bet["bet_type"] == "express" or len(legs) > 1:
            group = "express"
        else:
            group = (legs[0]["group"] if legs else None) or "other"
        status = bet["status"]
        settled = status in SETTLED and (bet["settled_at"] or "") >= since
        created = (bet["created_at"] or "") >= since and status not in VOID
        _add(total, bet, group, settled, created)
        for division_id in {leg["division_id"] for leg in legs} or {None}:
            _add(scopes.setdefault(division_id, _bucket()), bet, group, settled, created)

        u = users.setdefault(bet["user_id"], {
            "user_id": bet["user_id"], "username": bet.get("username"), "team_name": bet.get("team_name"),
            "profit": 0, "settled_bets": 0, "pending_stake": 0,
        })
        if settled:
            paid = int(bet["actual_payout"] or 0) if status in PAID else 0
            u["profit"] += paid - int(bet["amount"] or 0)
            u["settled_bets"] += 1
        if status == "pending":
            u["pending_stake"] += int(bet["amount"] or 0)

    names = {d["id"]: d["name"] for d in data["divisions"]}
    scope_rows = []
    for division_id, bucket in scopes.items():
        row = _finish(bucket)
        row["division_id"] = division_id
        row["name"] = names.get(division_id, f"Дивизион {division_id}") if division_id else GENERAL_CUP_LABEL
        scope_rows.append(row)
    # Дивизионы по порядку, общий кубок последним.
    scope_rows.sort(key=lambda r: (r["division_id"] is None, r["division_id"] or 0))

    winners = sorted((u for u in users.values() if u["profit"] > 0), key=lambda u: -u["profit"])
    positive = sum(u["profit"] for u in winners)
    top_winners = [{**u, "share_pct": round(u["profit"] * 100.0 / positive, 1) if positive else None}
                   for u in winners[:TOP_WINNERS]]
    top_pending = max(users.values(), key=lambda u: u["pending_stake"], default=None)

    flows = []
    rewards = bets_net = admin_net = 0
    for f in data["coin_flows"]:
        net = int(f["credited"]) - int(f["debited"])
        kind = f["transaction_type"]
        flows.append({"type": kind, "label": FLOW_LABELS.get(kind, kind), "count": f["cnt"],
                      "credited": int(f["credited"]), "debited": int(f["debited"]), "net": net})
        if kind in REWARD_FLOWS:
            rewards += net
        elif kind in BET_FLOWS:
            bets_net += net
        elif kind.startswith("admin_"):
            admin_net += net
    flows.sort(key=lambda f: -f["net"])

    return {
        "period_days": days,
        "since": since,
        "totals": _finish(total),
        "scopes": scope_rows,
        "top_winners": top_winners,
        "players_profit": positive,
        "top_pending": ({"user_id": top_pending["user_id"], "username": top_pending["username"],
                         "team_name": top_pending["team_name"], "pending_stake": top_pending["pending_stake"]}
                        if top_pending and top_pending["pending_stake"] > 0 else None),
        "coin_flows": flows,
        "inflow": {"rewards": rewards, "bets": bets_net, "admin": admin_net,
                   "total": sum(f["net"] for f in flows)},
        "wallets": data["wallets"],
    }


# ─── Текущие лимиты ─────────────────────────────────────────────────────────

def current_limits(division_ids: list[int]) -> dict:
    system = {
        **BettingLimitsService.get_system_limits(),
        "max_express_events": database.get_max_express_events(),
        "express_margin_pct": database.get_express_margin_pct(),
        "initial_balance": database.get_initial_wallet_balance(),
    }
    return {
        "global": system,
        "global_bans": sorted(database.get_bet_bans(None)),
        "divisions": {d: BettingLimitsService.get_division_limits(d) for d in division_ids},
        "division_bans": {d: sorted(database.get_bet_bans(d)) for d in division_ids},
    }


def _current_value(limits: dict, scope_type: str, scope_id: int, key: str) -> int | None:
    """Значение, которое сейчас действует на этом уровне; у запрета — 1 или 0."""
    if key.startswith(database.BET_BAN_PREFIX):
        group = key[len(database.BET_BAN_PREFIX):]
        if scope_type == "global":
            return 1 if group in limits["global_bans"] else 0
        return 1 if group in limits["division_bans"].get(scope_id, []) else 0
    if scope_type == "global":
        return limits["global"].get(key)
    if scope_type == "division":
        return limits["divisions"].get(scope_id, {}).get(key)
    if scope_type == "user":
        return BettingLimitsService.get_user_effective_limits(scope_id).get(key)
    return None


def is_stricter(key: str, value: int, current: int | None) -> bool:
    if current is None:
        return True
    if key.startswith(database.BET_BAN_PREFIX) or key in STRICTER_WHEN_HIGHER:
        return value > current
    return value < current


# ─── Правила ────────────────────────────────────────────────────────────────

def _floor_to(value: float, step: int) -> int:
    return int(value // step * step)


def _ceil_to(value: float, step: int) -> int:
    return int(math.ceil(value / step) * step)


def _suggestion(scope_type: str, scope_id: int, key: str, value: int, reason: str,
                severity: str = "medium") -> dict:
    return {"scope_type": scope_type, "scope_id": scope_id, "limit_key": key,
            "value": int(value), "reason": reason, "severity": severity}


def rule_suggestions(stats: dict, limits: dict) -> list[dict]:
    out: list[dict] = []
    days = stats["period_days"]

    for scope in stats["scopes"]:
        division_id = scope["division_id"]
        if division_id is None or division_id not in limits["divisions"]:
            continue
        if scope["settled_stake"] < MIN_SCOPE_STAKE or scope["margin_pct"] is None:
            continue
        name = scope["name"]
        eff = limits["divisions"][division_id]

        for g in scope["groups"]:
            if g["group"] in ("result", "other") or g["stake"] < MIN_GROUP_STAKE:
                continue
            if g["margin_pct"] is not None and g["margin_pct"] <= BAN_MARGIN:
                out.append(_suggestion(
                    "division", division_id, database.BET_BAN_PREFIX + g["group"], 1,
                    f"{name}, {g['label'].lower()}: игроки в плюсе на {fmt_coins(-g['ggr'])} 🪙 "
                    f"при ставках на {fmt_coins(g['stake'])} 🪙 за {days} дн. (маржа {g['margin_pct']}%).",
                    "high"))

        margin = scope["margin_pct"]
        if margin > LOSS_MARGIN:
            continue
        high = margin <= HIGH_LOSS_MARGIN
        severity = "high" if high else "medium"
        why = (f"{name}: игроки в плюсе на {fmt_coins(-scope['ggr'])} 🪙 за {days} дн. "
               f"(маржа {margin}%).")

        payout = max(1_000, _floor_to(eff["max_payout"] * (0.5 if high else 0.75), 500))
        if payout < eff["max_payout"]:
            out.append(_suggestion("division", division_id, "max_payout", payout,
                                   why + " Меньше потолок — меньше крупных заносов.", severity))

        # Потолок ставки — только при большом минусе: дело обычно в кэфах, а не
        # в суммах, и срезать ставку всем ради пары крупных купонов незачем.
        p = scope["stake_p95"]
        if high and p:
            max_bet = max(MIN_SUGGESTED_MAX_BET, _ceil_to(p, 100))
            if max_bet < eff["max_bet"] * 0.9:
                out.append(_suggestion("division", division_id, "max_bet", max_bet,
                                       why + " Потолок по самым крупным 5% ставок дивизиона.", severity))

        if high and eff["max_open_bets"] > 3:
            open_bets = max(3, math.ceil(eff["max_open_bets"] * 0.6))
            if open_bets < eff["max_open_bets"]:
                out.append(_suggestion("division", division_id, "max_open_bets", open_bets,
                                       why + " Меньше купонов в игре одновременно.", severity))

    totals = stats["totals"]
    express = next((g for g in totals["groups"] if g["group"] == "express"), None)
    pct = limits["global"]["express_margin_pct"]
    if (express and express["stake"] >= 2 * MIN_GROUP_STAKE and express["margin_pct"] is not None
            and express["margin_pct"] <= LOSS_MARGIN and pct < database.MAX_EXPRESS_MARGIN_PCT):
        high = express["margin_pct"] <= HIGH_LOSS_MARGIN
        new_pct = min(database.MAX_EXPRESS_MARGIN_PCT, pct + (5 if high else 3))
        out.append(_suggestion(
            "global", 0, "express_margin_pct", new_pct,
            f"Экспрессы во всех дивизионах: игроки в плюсе на {fmt_coins(-express['ggr'])} 🪙 "
            f"(маржа {express['margin_pct']}%). Надбавка режет кэф за каждое событие после первого.",
            "high" if high else "medium"))

    cup = next((s for s in stats["scopes"] if s["division_id"] is None), None)
    if (cup and cup["settled_stake"] >= MIN_SCOPE_STAKE and cup["margin_pct"] is not None
            and cup["margin_pct"] <= LOSS_MARGIN):
        current = limits["global"]["max_payout"]
        payout = max(2_000, _floor_to(current * 0.75, 500))
        if payout < current:
            out.append(_suggestion(
                "global", 0, "max_payout", payout,
                f"{GENERAL_CUP_LABEL}: игроки в плюсе на {fmt_coins(-cup['ggr'])} 🪙 (маржа {cup['margin_pct']}%). "
                "Своих лимитов у него нет — действует общий потолок; у дивизионов со своим потолком он не меняется.",
                "high" if cup["margin_pct"] <= HIGH_LOSS_MARGIN else "medium"))

    losing = totals["margin_pct"] is not None and totals["margin_pct"] <= LOSS_MARGIN
    top_pending = stats.get("top_pending")
    exposure = limits["global"]["max_open_exposure"]
    if losing and top_pending and top_pending["pending_stake"] >= 0.8 * exposure:
        new_exposure = max(5_000, _floor_to(exposure * 0.75, 1_000))
        if new_exposure < exposure:
            out.append(_suggestion(
                "global", 0, "max_open_exposure", new_exposure,
                f"Игроки в плюсе по всем дивизионам (маржа {totals['margin_pct']}%), а у "
                f"{_player_name(top_pending)} в открытых купонах {fmt_coins(top_pending['pending_stake'])} 🪙 "
                f"из {fmt_coins(exposure)} 🪙 допустимых."))

    for w in stats["top_winners"][:1]:
        if w["profit"] >= WINNER_MIN_PROFIT and (w["share_pct"] or 0) >= WINNER_MIN_SHARE:
            current = BettingLimitsService.get_user_effective_limits(w["user_id"])["max_payout"]
            payout = max(1_000, _floor_to(current * 0.5, 500))
            if payout < current:
                out.append(_suggestion(
                    "user", w["user_id"], "max_payout", payout,
                    f"{_player_name(w)} забрал {w['share_pct']}% всей прибыли игроков за {days} дн. "
                    f"(+{fmt_coins(w['profit'])} 🪙). Личный потолок выигрыша только для него."))

    return _dedupe(out)[:MAX_SUGGESTIONS]


def _dedupe(items: list[dict]) -> list[dict]:
    seen = set()
    out = []
    for s in items:
        key = (s["scope_type"], s["scope_id"], s["limit_key"])
        if key not in seen:
            seen.add(key)
            out.append(s)
    return out


def _player_name(p: dict) -> str:
    if p.get("username"):
        return "@" + str(p["username"]).lstrip("@")
    return p.get("team_name") or f"id {p['user_id']}"


def rule_findings(stats: dict) -> list[dict]:
    """Выводы по цифрам — показываются всегда, и с ИИ, и без."""
    days = stats["period_days"]
    out = []
    healthy = []
    for scope in stats["scopes"]:
        if scope["settled_stake"] < MIN_SCOPE_STAKE or scope["margin_pct"] is None:
            continue
        if scope["margin_pct"] <= LOSS_MARGIN:
            worst = next((g for g in scope["groups"] if g["ggr"] < 0), None)
            text = (f"{scope['name']}: игроки в плюсе на {fmt_coins(-scope['ggr'])} 🪙 "
                    f"при ставках на {fmt_coins(scope['settled_stake'])} 🪙 (маржа {scope['margin_pct']}%).")
            if worst:
                text += f" Больше всего — {worst['label'].lower()}: {fmt_coins(-worst['ggr'])} 🪙."
            out.append({"severity": "high" if scope["margin_pct"] <= HIGH_LOSS_MARGIN else "medium",
                        "text": text})
        elif scope["ggr"] >= 0:
            healthy.append(scope["name"])
    if healthy:
        out.append({"severity": "info", "text": "В плюсе у букмекера: " + ", ".join(healthy) + "."})

    inflow = stats["inflow"]
    if inflow["rewards"] > 0:
        severity = "high" if inflow["rewards"] > max(inflow["bets"], 0) * 2 else "info"
        out.append({"severity": severity, "text": (
            f"За {days} дн. награды (достижения, уровни, бонусы) дали игрокам {fmt_coins(inflow['rewards'])} 🪙, "
            f"ставки — {'+' if inflow['bets'] >= 0 else '−'}{fmt_coins(abs(inflow['bets']))} 🪙 "
            "(вместе с суммами в ещё не рассчитанных купонах). "
            "Приток от наград лимитами ставок не остановить.")})

    top = stats["top_winners"][:1]
    if top and top[0]["profit"] >= WINNER_MIN_PROFIT and (top[0]["share_pct"] or 0) >= WINNER_MIN_SHARE:
        w = top[0]
        out.append({"severity": "medium", "text": (
            f"{_player_name(w)} — {w['share_pct']}% всей прибыли игроков (+{fmt_coins(w['profit'])} 🪙).")})

    totals = stats["totals"]
    if totals["settled_stake"] < MIN_SCOPE_STAKE:
        out.append({"severity": "info", "text": (
            f"За {days} дн. рассчитано купонов лишь на {fmt_coins(totals['settled_stake'])} 🪙 — "
            "выводы по такой выборке ненадёжны.")})
    return out


# ─── Проверка предложений ───────────────────────────────────────────────────

def validate_suggestion(raw, limits: dict, known_users: set[int]) -> dict | None:
    """Предложение модели → то же, что прошло бы POST /limits, и только ужесточение; иначе None."""
    if not isinstance(raw, dict):
        return None
    scope_type = raw.get("scope_type")
    if scope_type not in LIMIT_KEYS_BY_SCOPE:
        return None
    try:
        scope_id = 0 if scope_type == "global" else int(raw.get("scope_id"))
        value = raw.get("value")
        if isinstance(value, bool):
            return None
        value = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    if scope_type == "division" and scope_id not in limits["divisions"]:
        return None
    if scope_type == "user" and scope_id not in known_users:
        return None

    key = raw.get("limit_key")
    is_ban = scope_type in BAN_SCOPES and key in database.BET_BAN_KEYS
    if key not in LIMIT_KEYS_BY_SCOPE[scope_type] and not is_ban:
        return None
    low, high = (1, 1) if is_ban else LIMIT_BOUNDS.get(key, DEFAULT_LIMIT_BOUNDS)
    if not (low <= value <= high):
        return None
    if key == "min_bet" and value > limits["global"]["max_bet"]:
        return None
    if key == "max_bet" and value < limits["global"]["min_bet"]:
        return None
    current = _current_value(limits, scope_type, scope_id, key)
    if not is_stricter(key, value, current):
        return None

    reason = raw.get("reason")
    reason = reason.strip()[:REASON_MAX_CHARS] if isinstance(reason, str) else ""
    severity = raw.get("severity") if raw.get("severity") in ("high", "medium", "low") else "medium"
    return _suggestion(scope_type, scope_id, key, value, reason, severity)


def annotate(suggestions: list[dict], limits: dict, stats: dict) -> list[dict]:
    """Текущее значение, подписи и «уже стоит» — заново при каждом ответе, в том числе из кэша."""
    scope_names = {s["division_id"]: s["name"] for s in stats["scopes"] if s["division_id"] is not None}
    players = {w["user_id"]: w for w in stats["top_winners"]}
    if stats.get("top_pending"):
        players.setdefault(stats["top_pending"]["user_id"], stats["top_pending"])
    out = []
    for s in suggestions:
        key = s["limit_key"]
        current = _current_value(limits, s["scope_type"], s["scope_id"], key)
        if s["scope_type"] == "global":
            scope_label = ALL_DIVISIONS_LABEL
        elif s["scope_type"] == "division":
            scope_label = scope_names.get(s["scope_id"]) or f"Дивизион {s['scope_id']}"
        else:
            scope_label = "Игрок " + _player_name(players.get(s["scope_id"], {"user_id": s["scope_id"]}))
        if key.startswith(database.BET_BAN_PREFIX):
            label = "Запрет: " + GROUP_LABELS.get(key[len(database.BET_BAN_PREFIX):], key)
        else:
            label = LIMIT_LABELS.get(key, key)
        out.append({**s, "current": current, "label": label, "scope_label": scope_label,
                    "is_ban": key.startswith(database.BET_BAN_PREFIX),
                    "applied": not is_stricter(key, s["value"], current)})
    return out


# ─── Модель ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — риск-аналитик виртуального букмекера Logovo.bet в киберфутбольном чемпионате (EA FC, играют люди, "
    "монеты виртуальные, реальных денег нет). Цель админа — успокоить рынок: сократить приток монет игрокам "
    "из ставок, не убив интерес к ставкам.\n"
    "Тебе дают статистику за период: маржа букмекера (GGR/ставки, минус — игроки в плюсе) по дивизионам и "
    "группам ставок, открытые купоны, крупнейших победителей, движение монет по типам операций, текущие лимиты, "
    "какие ключи можно менять на каком уровне и в каких границах, и предложения простых правил.\n"
    "Задача: коротко объясни ситуацию и предложи до {limit} изменений лимитов. Только ужесточение. Запрет вида "
    "ставок — ключ ban_<группа> со значением 1, уровни global или division. Уровень user — только для игроков "
    "из данных. Не выдумывай дивизионы и игроков. Маленькая выборка (мало рассчитанных ставок) — повод "
    "не спешить. Приток от наград лимитами ставок не остановить — скажи об этом, если он велик. "
    "Пиши «все дивизионы», а не «лига».\n"
    "Ответ — ТОЛЬКО JSON без пояснений и без markdown:\n"
    '{{"summary": "<3–6 предложений по-русски>", "risks": ["<коротко>"], '
    '"suggestions": [{{"scope_type": "global|division|user", "scope_id": <id, у global 0>, '
    '"limit_key": "<ключ>", "value": <целое>, "severity": "high|medium|low", '
    '"reason": "<по-русски, до 200 символов>"}}]}}'
)


def _payload_for_model(stats: dict, limits: dict, rules: list[dict]) -> dict:
    def scope_view(s: dict) -> dict:
        return {
            "division_id": s.get("division_id"),
            "name": s.get("name", ALL_DIVISIONS_LABEL),
            "settled_stake": s["settled_stake"], "paid_out": s["paid_out"], "ggr": s["ggr"],
            "margin_pct": s["margin_pct"], "settled_bets": s["settled_bets"], "bettors": s["bettors"],
            "pending_stake": s["pending_stake"], "pending_liability": s["pending_liability"],
            "stake_p90": s["stake_p90"],
            "groups": [{k: g[k] for k in ("group", "stake", "ggr", "margin_pct", "bets")} for g in s["groups"]],
        }

    return {
        "period_days": stats["period_days"],
        "all_divisions": scope_view(stats["totals"]),
        "scopes": [scope_view(s) for s in stats["scopes"]],
        "top_winners": [{"user_id": w["user_id"], "name": _player_name(w), "profit": w["profit"],
                         "share_pct": w["share_pct"], "settled_bets": w["settled_bets"]}
                        for w in stats["top_winners"]],
        "coin_flows": [{k: f[k] for k in ("label", "credited", "debited", "net")} for f in stats["coin_flows"]],
        "wallets": stats["wallets"],
        "limits": {
            "global": limits["global"],
            "global_bans": limits["global_bans"],
            "divisions": {str(d): {k: v[k] for k in LIMIT_KEYS_BY_SCOPE["division"]}
                          for d, v in limits["divisions"].items()},
            "division_bans": {str(d): b for d, b in limits["division_bans"].items() if b},
        },
        "allowed_keys": {scope: list(keys) for scope, keys in LIMIT_KEYS_BY_SCOPE.items()},
        "ban_keys": {"keys": list(database.BET_BAN_KEYS), "scopes": list(BAN_SCOPES)},
        "bounds": {k: list(v) for k, v in LIMIT_BOUNDS.items()},
        "default_bounds": list(DEFAULT_LIMIT_BOUNDS),
        "key_meanings": LIMIT_MEANINGS,
        "rule_suggestions": [{k: s[k] for k in ("scope_type", "scope_id", "limit_key", "value", "reason")}
                             for s in rules],
    }


def _call_model(stats: dict, limits: dict, rules: list[dict]) -> tuple[dict | None, str | None]:
    system = _SYSTEM_PROMPT.format(limit=MAX_SUGGESTIONS)
    user = "ДАННЫЕ (JSON):\n" + json.dumps(_payload_for_model(stats, limits, rules), ensure_ascii=False)
    return bet_picks.call_model_chain(system, user, tag="AI market analysis")


def parse_ai_report(data: dict, limits: dict, known_users: set[int]) -> dict:
    summary = data.get("summary")
    summary = summary.strip()[:SUMMARY_MAX_CHARS] if isinstance(summary, str) else ""
    risks = []
    for r in data.get("risks") or []:
        if isinstance(r, str) and r.strip():
            risks.append(r.strip()[:RISK_MAX_CHARS])
    suggestions = []
    raw = data.get("suggestions")
    for item in raw if isinstance(raw, list) else []:
        s = validate_suggestion(item, limits, known_users)
        if s is not None:
            suggestions.append(s)
    return {"summary": summary, "risks": risks[:MAX_RISKS],
            "suggestions": _dedupe(suggestions)[:MAX_SUGGESTIONS]}


# ─── Сборка и кэш ───────────────────────────────────────────────────────────

def build_analysis(days: int = DEFAULT_PERIOD) -> dict:
    since = (now_msk() - datetime.timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    data = database.get_market_analysis_data(since)
    stats = compute_stats(data, days, since)
    limits = current_limits([d["id"] for d in data["divisions"]])
    rules = rule_suggestions(stats, limits)
    result = {
        "source": "rules",
        "model": None,
        "ai_configured": bool(config.OPENROUTER_API_KEY and bet_picks._models()),
        "error": None,
        "generated_at": now_msk_str(),
        "period_days": days,
        "periods": list(PERIODS),
        "summary": "",
        "risks": [],
        "findings": rule_findings(stats),
        "stats": stats,
        "suggestions": rules,
        "suggestions_source": "rules",
    }
    if not result["ai_configured"]:
        result["error"] = "no_key"
        return result

    known_users = {w["user_id"] for w in stats["top_winners"]}
    if stats.get("top_pending"):
        known_users.add(stats["top_pending"]["user_id"])
    data, model = _call_model(stats, limits, rules)
    report = parse_ai_report(data, limits, known_users) if isinstance(data, dict) else None
    if not report or not report["summary"]:
        result["error"] = "ai_unavailable"
        return result
    result.update(source="ai", model=model, summary=report["summary"], risks=report["risks"])
    if report["suggestions"]:
        result.update(suggestions=report["suggestions"], suggestions_source="ai")
    return result


def _respond(data: dict, cached: bool, refresh_in: int) -> dict:
    """Кэш хранит предложения как есть; текущие значения и «уже стоит» — всегда свежие."""
    division_ids = [s["division_id"] for s in data["stats"]["scopes"] if s["division_id"] is not None]
    division_ids += [s["scope_id"] for s in data["suggestions"] if s["scope_type"] == "division"]
    limits = current_limits(sorted(set(division_ids)))
    return {**data, "suggestions": annotate(data["suggestions"], limits, data["stats"]),
            "cached": cached, "refresh_in": refresh_in}


def get_analysis(days: int = DEFAULT_PERIOD, refresh: bool = False) -> dict:
    """Анализ из кэша или свежий. Одновременно в модель идёт только один запрос."""
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(days)
    if cached:
        ts, data = cached
        age = now - ts
        ttl = CACHE_TTL_SECONDS if data["source"] == "ai" else FALLBACK_TTL_SECONDS
        if age < ttl and not (refresh and age >= REFRESH_MIN_SECONDS):
            return _respond(data, True, int(max(0, REFRESH_MIN_SECONDS - age)))

    with _call_lock:
        with _cache_lock:
            cached = _cache.get(days)
        if cached and cached[0] > now:
            return _respond(cached[1], True, REFRESH_MIN_SECONDS)
        data = build_analysis(days)
        with _cache_lock:
            _cache[days] = (time.monotonic(), data)
    return _respond(data, False, REFRESH_MIN_SECONDS)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
