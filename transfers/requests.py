"""Заявки трансферного окна: сделка, свободный агент, доплата, урна, отзыв и подтверждение.

Без Telegram и без HTTP: API Mini App и хендлеры зовут эти функции, а
уведомления отправляют сами. Ошибка человека — `InputError` с русским текстом.

Каждая заявка проверяется `engine.evaluate_request` перед записью и ещё раз,
когда её подтверждает вторая сторона: за это время мог измениться бюджет или
состав. Блокировка — заявка не записывается; предупреждения пишутся в заявку
и показываются ответственному.

Свободных агентов тренеры не подают: ответственный пересылает боту комментарий
из канала (`parse_fa_comment` → `fa_preview` → `record_free_agent`).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field, replace

import database
from club_registry import resolve_team_name
from services.graphics import player_photos
from time_utils import DT_FORMAT, parse_msk
from transfers import config as tcfg
from transfers import repo, sanctions, service
from transfers.engine import (
    ACTIVE_STATUSES,
    PENDING_STATUSES,
    SELL_SLOT_KINDS,
    SPEND_KINDS,
    Evaluation,
    Issue,
    RequestContext,
    TransferRequest,
    WindowSettings,
    core_remaining,
    evaluate_request,
    format_k,
    norm_club,
    norm_player,
    parse_money_k,
)
from transfers.service import InputError

URN_CLUB = "Урна"
COUNTERPARTY_DECLINED = "Отклонена второй стороной"
FA_REASSIGN_REASON = "Игрок переписан по более раннему комментарию"
PLAYER_NAME_MAX = 60
OVR_RANGE = (1, 199)
HISTORY_LIMIT = 200


# ─── Ввод ────────────────────────────────────────────────────────────────────

def _clean_player(name) -> str:
    text = " ".join(str(name or "").split())
    if not norm_player(text):
        raise InputError("Укажите игрока.")
    if len(text) > PLAYER_NAME_MAX:
        raise InputError("Слишком длинное имя игрока.")
    return text


def _parse_price(value, label: str) -> int:
    if value is None or str(value).strip() == "":
        raise InputError(f"Укажите {label}.")
    amount = parse_money_k(value)
    if amount is None:
        raise InputError(f"Не понял {label}: пример <code>12,5</code> (в млн).")
    return amount


def _parse_ovr(value, *, required: bool) -> int | None:
    raw = str(value if value is not None else "").strip()
    if not raw:
        if required:
            raise InputError("Укажите OVR карты.")
        return None
    if not raw.isdigit() or not OVR_RANGE[0] <= int(raw) <= OVR_RANGE[1]:
        raise InputError("OVR — целое число, например <code>105</code>.")
    return int(raw)


def _parse_id(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InputError("Заявка не найдена.") from None
    if number <= 0:
        raise InputError("Заявка не найдена.")
    return number


# ─── Окно и клуб тренера ─────────────────────────────────────────────────────

def _window() -> dict:
    window = repo.get_active_window()
    if window is None:
        raise InputError("Трансферное окно закрыто.")
    return window


def coach_club(user_id: int) -> str:
    """Клуб тренера каноническим именем; нет клуба — `InputError`."""
    club = _coach_club_or_none(user_id)
    if not club:
        raise InputError("За вами не закреплён клуб — заявки подают тренеры клубов лиги.")
    return club


def _coach_club_or_none(user_id: int | None) -> str | None:
    if not user_id:
        return None
    user = database.get_user(int(user_id))
    team = ((user["team_name"] if user else None) or "").strip()
    if not team:
        return None
    return resolve_team_name(team) or team


def _same_club(a: str | None, b: str | None) -> bool:
    ka, kb = norm_club(a), norm_club(b)
    return bool(ka) and ka == kb


# ─── Проверка ────────────────────────────────────────────────────────────────

def _league_sides(req: TransferRequest) -> tuple[str | None, str | None]:
    """(продавец, покупатель) из клубов лиги — у кого считать бюджет, слоты и ядро."""
    seller = req.from_club if req.kind in SELL_SLOT_KINDS else None
    buyer = req.to_club if req.kind in SPEND_KINDS else None
    return seller, buyer


def _fa_order(t: dict) -> tuple:
    """Порядок свободных агентов: раньше комментарий — раньше запись."""
    return (t.get("commented_at") or t.get("created_at") or "", t["id"])


def _check(window: dict, req: TransferRequest, *, exclude_id: int | None = None,
           urn_item: dict | None = None, user_ids: tuple = ()) -> Evaluation:
    """Собрать контекст из базы и проверить заявку.

    `exclude_id` — сама заявка (повторная проверка) или запись, которую она
    заменяет; её нет ни в бюджетах, ни в счётчиках.
    """
    wid = window["id"]
    settings = WindowSettings.from_row(window)
    seller_club, buyer_club = _league_sides(req)
    active = [t for t in repo.list_transfers(wid, statuses=ACTIVE_STATUSES) if t["id"] != exclude_id]
    buyer = repo.get_club_ledger(wid, buyer_club, exclude_id) if buyer_club else None
    seller = repo.get_club_ledger(wid, seller_club, exclude_id) if seller_club else None

    season = window.get("season_id")
    sanctioned = bool(season) and (
        any(repo.is_sanctioned(int(season), club_name=c) for c in (seller_club, buyer_club) if c)
        or any(repo.is_sanctioned(int(season), user_id=int(u)) for u in user_ids if u))

    key = norm_player(req.player_name)
    user_free_agents, fa_taken_by = 0, None
    if req.kind == "free_agent":
        coach = user_ids[0] if user_ids else None
        fas = [t for t in active if t["kind"] == "free_agent"]
        user_free_agents = sum(1 for t in fas if (coach and t["to_user"] == coach)
                               or _same_club(t["to_club"], buyer_club))
        same_player = sorted((t for t in fas if t["norm_name"] == key), key=_fa_order)
        fa_taken_by = same_player[0] if same_player else None

    core_after = in_squad = None
    if seller_club:
        squad = service.club_squad(seller_club)
        if squad is not None:
            in_squad = key in {norm_player(n) for n in squad}
            snapshot = [r["player_name"] for r in repo.get_core_snapshot(wid, seller_club)]
            if snapshot:
                outgoing = [t["player_name"] for t in active
                            if t["kind"] in SELL_SLOT_KINDS and _same_club(t["from_club"], seller_club)]
                core_after = core_remaining(snapshot, squad, outgoing + [req.player_name])

    taken = urn_item is not None and any(
        t["kind"] == "urn_buy" and t["urn_item_id"] == urn_item["id"] for t in active)

    ctx = RequestContext(
        settings=settings, buyer=buyer, seller=seller, directory=repo.get_player(req.player_name),
        sanctioned=sanctioned, user_free_agents=user_free_agents, fa_taken_by=fa_taken_by,
        seller_core_after=core_after, player_in_seller_squad=in_squad,
        urn_item=urn_item, urn_item_taken=taken,
    )
    return evaluate_request(req, ctx)


def _require(ev: Evaluation) -> Evaluation:
    if not ev.ok:
        raise InputError("\n".join(issue.message for issue in ev.blocks))
    return ev


def _warnings(ev: Evaluation) -> list[dict]:
    return [w.as_dict() for w in ev.warnings]


def _no_duplicate(window_id: int, req: TransferRequest, exclude_id: int | None = None) -> None:
    """Тот же игрок уже в неподтверждённой заявке той же стороны — вторую не принимаем."""
    key = norm_player(req.player_name)
    for t in repo.list_transfers(window_id, statuses=PENDING_STATUSES):
        if t["id"] == exclude_id or t["norm_name"] != key:
            continue
        if req.kind in SELL_SLOT_KINDS and t["kind"] in SELL_SLOT_KINDS \
                and _same_club(t["from_club"], req.from_club):
            raise InputError(f"На {req.player_name} уже есть заявка #{t['id']}.")
        if req.kind == "surcharge" and t["kind"] == "surcharge" and _same_club(t["to_club"], req.to_club):
            raise InputError(f"Доплата за {req.player_name} уже подана: заявка #{t['id']}.")


def _request_from_row(t: dict) -> TransferRequest:
    return TransferRequest(
        kind=t["kind"], player_name=t["player_name"], from_club=t["from_club"], to_club=t["to_club"],
        price_k=t["price_k"], ovr=t["ovr"], tm_price_k=t["tm_price_k"],
        special_price_k=t["special_price_k"], sellable=t["sellable"] != 0,
        commented_at=t["commented_at"], reported_budget_k=t["reported_budget_k"],
    )


# ─── Подача заявок тренером ──────────────────────────────────────────────────

def create_deal(user_id: int, *, role: str, other_club: str, player: str, price, ovr) -> dict:
    """Сделка между двумя тренерами. Подаёт любая сторона, вторая подтверждает.

    `role` — `buy` (тренер покупает у `other_club`) или `sell` (продаёт ему).
    """
    if role not in ("buy", "sell"):
        raise InputError("Укажите, покупаете вы игрока или продаёте.")
    own = coach_club(user_id)
    other = service.resolve_club(other_club or "")
    if _same_club(own, other):
        raise InputError("Нельзя заключить сделку со своим же клубом.")
    counterparty = database.find_user_by_team(other)
    if not counterparty or not counterparty.get("telegram_id"):
        raise InputError(f"У клуба {other} нет тренера в боте — сделку некому подтвердить.")
    other_id = int(counterparty["telegram_id"])
    name, price_k, ovr_v = _clean_player(player), _parse_price(price, "цену"), _parse_ovr(ovr, required=True)

    if role == "buy":
        buyer, seller, buyer_id, seller_id = own, other, int(user_id), other_id
    else:
        buyer, seller, buyer_id, seller_id = other, own, other_id, int(user_id)
    req = TransferRequest("deal", name, from_club=seller, to_club=buyer, price_k=price_k, ovr=ovr_v)
    with database.transaction():
        window = _window()
        _no_duplicate(window["id"], req)
        ev = _require(_check(window, req, user_ids=(buyer_id, seller_id)))
        tid = repo.insert_transfer(
            window["id"], "deal", name, "pending_counterparty", _warnings(ev),
            from_club=seller, to_club=buyer, from_user=seller_id, to_user=buyer_id,
            price_k=price_k, ovr=ovr_v, initiator_id=int(user_id))
        return repo.get_transfer(tid)


def create_surcharge(user_id: int, *, player: str, ovr) -> dict:
    """Доплата за спешл своего игрока: цена — по таблице окна, слот не тратит."""
    own = coach_club(user_id)
    name, ovr_v = _clean_player(player), _parse_ovr(ovr, required=True)
    req = TransferRequest("surcharge", name, to_club=own, ovr=ovr_v)
    with database.transaction():
        window = _window()
        _no_duplicate(window["id"], req)
        ev = _require(_check(window, req, user_ids=(int(user_id),)))
        tid = repo.insert_transfer(
            window["id"], "surcharge", name, "pending_manager", _warnings(ev),
            to_club=own, to_user=int(user_id), price_k=ev.price_k, ovr=ovr_v, initiator_id=int(user_id))
        return repo.get_transfer(tid)


def create_urn_sale(user_id: int, *, player: str, tm_price, special_price, sellable: bool = True) -> dict:
    """Продажа своего игрока в урну: выплата по формуле окна, тратит слот продажи."""
    own = coach_club(user_id)
    name = _clean_player(player)
    tm_k = _parse_price(tm_price, "цену по Transfermarkt")
    special_k = _parse_price(special_price, "цену за спешл")
    req = TransferRequest("urn_sale", name, from_club=own, tm_price_k=tm_k,
                          special_price_k=special_k, sellable=bool(sellable))
    with database.transaction():
        window = _window()
        _no_duplicate(window["id"], req)
        ev = _require(_check(window, req, user_ids=(int(user_id),)))
        tid = repo.insert_transfer(
            window["id"], "urn_sale", name, "pending_manager", _warnings(ev),
            from_club=own, from_user=int(user_id), price_k=ev.price_k, tm_price_k=tm_k,
            special_price_k=special_k, sellable=bool(sellable), initiator_id=int(user_id))
        return repo.get_transfer(tid)


def create_urn_buy(user_id: int, *, urn_item_id) -> dict:
    """Выкуп игрока из урны этого окна: цена — TM + спешл, тратит слот покупки."""
    own = coach_club(user_id)
    item_id = _parse_id(urn_item_id)
    with database.transaction():
        window = _window()
        item = repo.get_transfer(item_id)
        if item is None or item["window_id"] != window["id"]:
            raise InputError("Этого игрока нет в урне.")
        req = TransferRequest("urn_buy", item["player_name"], from_club=URN_CLUB, to_club=own,
                              ovr=item["ovr"])
        ev = _require(_check(window, req, urn_item=item, user_ids=(int(user_id),)))
        tid = repo.insert_transfer(
            window["id"], "urn_buy", item["player_name"], "pending_manager", _warnings(ev),
            from_club=URN_CLUB, to_club=own, to_user=int(user_id), price_k=ev.price_k,
            ovr=item["ovr"], tm_price_k=item["tm_price_k"], special_price_k=item["special_price_k"],
            sellable=item["sellable"], urn_item_id=item["id"], initiator_id=int(user_id))
        return repo.get_transfer(tid)


# ─── Вторая сторона и отзыв ──────────────────────────────────────────────────

def counterparty_id(t: dict) -> int | None:
    """Кто должен подтвердить сделку — сторона, которая её не подавала."""
    if t["kind"] != "deal":
        return None
    return t["to_user"] if t["initiator_id"] == t["from_user"] else t["from_user"]


def _pending_for_counterparty(user_id: int, transfer_id) -> dict:
    t = repo.get_transfer(_parse_id(transfer_id))
    if t is None or counterparty_id(t) != int(user_id):
        raise InputError("Заявка не найдена.")
    if t["status"] != "pending_counterparty":
        raise InputError("Заявка уже не ждёт вашего подтверждения.")
    return t


def confirm(user_id: int, transfer_id) -> dict:
    """Вторая сторона согласна: перепроверка и — к ответственному."""
    with database.transaction():
        t = _pending_for_counterparty(user_id, transfer_id)
        window = repo.get_window(t["window_id"])
        if window is None or window["status"] != "open":
            raise InputError("Трансферное окно закрыто.")
        ev = _require(_check(window, _request_from_row(t), exclude_id=t["id"],
                             user_ids=(t["to_user"], t["from_user"])))
        if not repo.set_transfer_status(t["id"], "pending_manager", expected=("pending_counterparty",)):
            raise InputError("Заявка уже не ждёт вашего подтверждения.")
        repo.set_transfer_warnings(t["id"], _warnings(ev))
        return repo.get_transfer(t["id"])


def decline(user_id: int, transfer_id) -> dict:
    with database.transaction():
        t = _pending_for_counterparty(user_id, transfer_id)
        if not repo.set_transfer_status(t["id"], "rejected", expected=("pending_counterparty",),
                                        actor_id=int(user_id), reason=COUNTERPARTY_DECLINED):
            raise InputError("Заявка уже не ждёт вашего подтверждения.")
        return repo.get_transfer(t["id"])


def withdraw(user_id: int, transfer_id) -> dict:
    """Подавший отзывает заявку, пока по ней не решили."""
    with database.transaction():
        t = repo.get_transfer(_parse_id(transfer_id))
        if t is None or t["initiator_id"] != int(user_id):
            raise InputError("Заявка не найдена.")
        if t["status"] not in PENDING_STATUSES:
            raise InputError("По заявке уже решили — отозвать её нельзя.")
        if not repo.set_transfer_status(t["id"], "withdrawn", expected=PENDING_STATUSES):
            raise InputError("По заявке уже решили — отозвать её нельзя.")
        return repo.get_transfer(t["id"])


# ─── Что видит тренер ────────────────────────────────────────────────────────

def _loads(raw) -> list:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def portrait_url(player_name: str | None, *clubs: str | None) -> str | None:
    """Ссылка на портрет игрока из кэша `assets/players/`; без сети и без записи.

    Портрет кэшируется под именем и клубом, где игрока опознали, поэтому пробуем клубы сделки
    по очереди, а `get_photo_path` сам откатывается к файлу без клуба. Нет файла — `None`,
    и Mini App рисует монограмму.
    """
    if not player_name:
        return None
    try:
        # `get_photo_path` с клубом откатывается к файлу без клуба, поэтому сначала проверяем
        # файлы по клубам напрямую, и только потом общий.
        candidates = [player_photos.get_cached_photo_path(player_name, club)
                      for club in clubs if club and norm_club(club) != norm_club(URN_CLUB)]
        candidates.append(player_photos.get_cached_photo_path(player_name, None))
        for path in candidates:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return "/assets/players/" + urllib.parse.quote(os.path.basename(path))
    except Exception:
        return None
    return None


def prefetch_portrait(t: dict) -> str | None:
    """Скачать портрет игроку одобренной заявки в кэш `assets/players/`. Блокирует — зовите в потоке.

    Игрока опознают по ростеру реального клуба, а тот, откуда он ушёл, и есть его клуб, поэтому
    пробуем `from_club`, затем `to_club`; «Урна» клубом не считается. Уже есть файл — сети нет.
    Никогда не бросает: нет портрета — Mini App рисует монограмму.
    """
    name = t.get("player_name")
    clubs = [c for c in (t.get("from_club"), t.get("to_club"))
             if c and norm_club(c) != norm_club(URN_CLUB)]
    if not name or not clubs or portrait_url(name, *clubs):
        return None
    for club in clubs:
        try:
            path = player_photos.fetch_and_cache(name, club)
        except Exception:
            continue
        if path:
            return path
    return None


def serialize(t: dict, viewer_id: int | None = None, *, private: bool = False) -> dict:
    """Заявка для Mini App. Без Telegram ID и текста комментария.

    `private` — заявка самого тренера: с предупреждениями и причиной отказа.
    """
    viewer = int(viewer_id) if viewer_id else None
    data = {
        "id": t["id"], "kind": t["kind"], "status": t["status"],
        "player_name": t["player_name"], "from_club": t["from_club"], "to_club": t["to_club"],
        "price_k": t["price_k"], "price": format_k(t["price_k"]), "ovr": t["ovr"],
        "tm_price_k": t["tm_price_k"], "special_price_k": t["special_price_k"],
        "sellable": None if t["sellable"] is None else bool(t["sellable"]),
        "commented_at": t["commented_at"], "created_at": t["created_at"], "decided_at": t["decided_at"],
        "has_photo": bool(t["photo_file_id"]),
        "portrait_url": portrait_url(t["player_name"], t["from_club"], t["to_club"]),
    }
    if private:
        data["warnings"] = _loads(t["warnings"])
        data["decided_reason"] = t["decided_reason"]
        data["is_initiator"] = viewer is not None and t["initiator_id"] == viewer
        data["can_withdraw"] = data["is_initiator"] and t["status"] in PENDING_STATUSES
        data["can_confirm"] = (t["status"] == "pending_counterparty" and viewer is not None
                               and counterparty_id(t) == viewer)
    return data


def _involves(t: dict, user_id: int, club: str | None) -> bool:
    if user_id in (t["from_user"], t["to_user"], t["initiator_id"]):
        return True
    return bool(club) and (_same_club(t["from_club"], club) or _same_club(t["to_club"], club))


def _window_info(window: dict) -> dict:
    return {key: window.get(key) for key in
            ("id", "title", "status", "opened_at", "closed_at", "auto_close_at", "fa_opens_at")}


def _rules(settings: WindowSettings) -> dict:
    return {
        "ovr_cap": settings.ovr_cap,
        "min_core_players": settings.min_core_players,
        "surcharge_min_ovr": settings.surcharge_min_ovr,
        "surcharge_table": [{"ovr": ovr, "price_k": price}
                            for ovr, price in sorted(settings.surcharge_table.items())],
        "urn_divisor_sellable": settings.urn_divisor_sellable,
        "urn_divisor_unsellable": settings.urn_divisor_unsellable,
        "urn_step_k": tcfg.URN_ROUNDING_K,
        "urn_max_per_club": settings.urn_max_per_club,
    }


def my_status(user_id: int) -> dict:
    """Экран «Статус»: окно, бюджет и слоты клуба, заявки тренера."""
    window = repo.get_active_window()
    club = _coach_club_or_none(user_id)
    if window is None:
        latest = repo.get_latest_window()
        return {"window": None, "latest": _window_info(latest) if latest else None, "club": club,
                "ledger": None, "requests": [], "rules": None,
                "sanction": sanctions.for_user(user_id, club)}
    settings = WindowSettings.from_row(window)
    ledger = None
    if club:
        lg = repo.get_club_ledger(window["id"], club)
        ledger = {
            "budget_k": lg.budget_k, "spent_k": lg.spent_k, "earned_k": lg.earned_k,
            "remaining_k": lg.remaining_k, "budget_set": repo.get_club_budget(window["id"], club) is not None,
            "buys_used": lg.buys_used, "buys_limit": lg.buys_limit,
            "sells_used": lg.sells_used, "sells_limit": lg.sells_limit,
            "urn_sales": lg.urn_sales, "urn_limit": settings.urn_max_per_club,
        }
    own = [t for t in repo.list_transfers(window["id"]) if _involves(t, int(user_id), club)]
    own.sort(key=lambda t: t["id"], reverse=True)
    return {"window": _window_info(window), "club": club, "ledger": ledger,
            "requests": [serialize(t, user_id, private=True) for t in own], "rules": _rules(settings),
            "sanction": sanctions.for_user(user_id, club)}


def urn_items(user_id: int) -> dict:
    """Урна окна: одобренные продажи, которые ещё никто не выкупает."""
    window = repo.get_active_window()
    if window is None:
        return {"window": None, "items": [], "can_buy": False}
    transfers = repo.list_transfers(window["id"])
    busy = {t["urn_item_id"] for t in transfers
            if t["kind"] == "urn_buy" and t["status"] in ACTIVE_STATUSES}
    club = _coach_club_or_none(user_id)
    items = []
    for t in transfers:
        if t["kind"] != "urn_sale" or t["status"] != "approved" or t["id"] in busy:
            continue
        data = serialize(t)
        buy_k = int(t["tm_price_k"] or 0) + int(t["special_price_k"] or 0)
        data.update(buy_price_k=buy_k, buy_price=format_k(buy_k))
        items.append(data)
    return {"window": _window_info(window), "items": items,
            "can_buy": window["status"] == "open" and bool(club)}


def _history_window(window_id: int | None) -> dict | None:
    """Окно истории: запрошенное, а нет такого или не задано — текущее либо последнее."""
    if window_id:
        window = repo.get_window(int(window_id))
        if window is not None:
            return window
    return repo.get_active_window() or repo.get_latest_window()


def history(user_id: int | None = None, *, window_id: int | None = None, mine: bool = False,
            club: str | None = None, limit: int = HISTORY_LIMIT) -> dict:
    """История окна, свежие сверху.

    По умолчанию — одобренные заявки текущего (или последнего) окна, публично. `window_id` —
    другое окно из списка `windows`. `mine` — все заявки зрителя и его клуба в любом статусе,
    с причинами отказа. `club` — только заявки, где клуб продавец или покупатель.
    """
    windows = [_window_info(w) for w in repo.list_windows()]
    window = _history_window(window_id)
    if window is None:
        return {"window": None, "windows": windows, "items": [], "clubs": [], "mine": False, "club": None}
    viewer_club = _coach_club_or_none(user_id)
    mine = bool(mine) and user_id is not None
    if mine:
        pool = [t for t in repo.list_transfers(window["id"]) if _involves(t, int(user_id), viewer_club)]
    else:
        pool = repo.list_transfers(window["id"], statuses=("approved",))
    clubs = sorted({c for t in pool for c in (t["from_club"], t["to_club"])
                    if c and norm_club(c) != norm_club(URN_CLUB)}, key=str.lower)
    wanted = (club or "").strip()
    if wanted:
        pool = [t for t in pool if _same_club(t["from_club"], wanted) or _same_club(t["to_club"], wanted)]
    pool.sort(key=lambda t: (t["decided_at"] or t["created_at"] or "", t["id"]), reverse=True)
    items = [serialize(t, user_id, private=True) if mine else serialize(t) for t in pool[:limit]]
    return {"window": _window_info(window), "windows": windows, "items": items, "clubs": clubs,
            "mine": mine, "club": wanted or None}


def photo_file_id(user_id: int, transfer_id) -> str | None:
    """`file_id` фото, если зрителю можно его видеть: одобренная заявка, своя или ответственному."""
    t = repo.get_transfer(_parse_id(transfer_id))
    if t is None or not t["photo_file_id"]:
        return None
    if t["status"] == "approved" or service.can_manage_window(user_id) \
            or _involves(t, int(user_id), _coach_club_or_none(user_id)):
        return t["photo_file_id"]
    return None


# ─── Свободные агенты: комментарий из канала ─────────────────────────────────

_FA_LINE = re.compile(r"^\s*(\d)\s*[.)]\s*(.*?)\s*$")
_FA_OVR = re.compile(r"\bovr\s*[:=\-]?\s*(\d{2,3})\b", re.IGNORECASE)
_FA_REQUIRED = {1: "имя игрока", 3: "куда", 4: "сумма"}


def _numbered_lines(text: str | None) -> dict[int, str]:
    lines: dict[int, str] = {}
    for line in str(text or "").splitlines():
        match = _FA_LINE.match(line)
        if match and int(match[1]) not in lines:
            lines[int(match[1])] = match[2]
    return lines


def looks_like_fa(text: str | None) -> bool:
    """Похоже на комментарий-заявку СА: есть пункты 1, 3 и 4."""
    return set(_FA_REQUIRED) <= set(_numbered_lines(text))


@dataclass
class FaDraft:
    """Разобранный комментарий СА, ещё не записанный."""
    player_name: str
    from_club: str | None
    to_text: str
    to_club: str
    to_user: int | None
    price_k: int
    reported_budget_k: int | None
    ovr: int | None
    commented_at: str | None
    source_text: str
    photo_file_id: str | None = None

    def request(self) -> TransferRequest:
        return TransferRequest(
            "free_agent", self.player_name, from_club=self.from_club, to_club=self.to_club,
            price_k=self.price_k, ovr=self.ovr, commented_at=self.commented_at,
            reported_budget_k=self.reported_budget_k)


def commented_at_msk(moment: dt.datetime | None) -> str | None:
    """Дата пересланного комментария (`forward_origin.date`, aware UTC) → строка МСК."""
    if moment is None:
        return None
    local = parse_msk(moment)
    return local.strftime(DT_FORMAT) if local else None


def parse_fa_comment(text: str | None, *, commented_at: str | None = None,
                     photo_file_id: str | None = None) -> FaDraft:
    """«1. Имя Фамилия / 2. Откуда / 3. Куда / 4. Сумма / 5. Остаток / 6. Фото» → черновик.

    Разбор детерминированный: номера пунктов, клуб «Куда» — через реестр клубов,
    тренер — по клубу. OVR — необязательное «OVR 105» в любом месте.
    """
    raw = str(text or "").strip()
    lines = _numbered_lines(raw)
    missing = [label for num, label in _FA_REQUIRED.items() if not lines.get(num)]
    if missing:
        raise InputError("В комментарии нет пунктов: " + ", ".join(missing) + ".")

    ovr_match = _FA_OVR.search(raw)
    ovr = int(ovr_match[1]) if ovr_match else None
    name = _FA_OVR.sub("", lines[1]).strip(" ,;-—")
    name = _clean_player(name)

    from_club = " ".join(_FA_OVR.sub("", lines.get(2, "")).split()) or None
    to_text = " ".join(lines[3].split())
    to_club = service.resolve_club(to_text)
    coach = database.find_user_by_team(to_club)
    price_k = parse_money_k(lines[4])
    if price_k is None:
        raise InputError(f"Не понял сумму: «{lines[4]}».")
    reported = parse_money_k(lines[5]) if lines.get(5) else None
    return FaDraft(
        player_name=name, from_club=from_club, to_text=to_text, to_club=to_club,
        to_user=int(coach["telegram_id"]) if coach and coach.get("telegram_id") else None,
        price_k=price_k, reported_budget_k=reported, ovr=ovr, commented_at=commented_at,
        source_text=raw, photo_file_id=photo_file_id)


@dataclass
class FaPreview:
    draft: FaDraft
    evaluation: Evaluation
    conflict: dict | None = None          # запись позднего комментария на этого игрока
    duplicate: dict | None = None         # этот же комментарий уже записан
    notes: list[Issue] = field(default_factory=list)

    @property
    def can_record(self) -> bool:
        return self.evaluation.ok and self.duplicate is None and self.conflict is None

    @property
    def can_reassign(self) -> bool:
        return self.evaluation.ok and self.duplicate is None and self.conflict is not None


def _fa_records(window_id: int, draft: FaDraft) -> list[dict]:
    key = norm_player(draft.player_name)
    return sorted((t for t in repo.list_transfers(window_id, kinds=("free_agent",), statuses=ACTIVE_STATUSES)
                   if t["norm_name"] == key), key=_fa_order)


def fa_preview(draft: FaDraft) -> FaPreview:
    """Проверка СА перед записью. Ничего не пишет.

    Если игрок уже записан по комментарию, оставленному **позже** этого, —
    `conflict`: побеждает ранний, ответственному предлагается переписать.
    """
    window = _window()
    records = _fa_records(window["id"], draft)
    duplicate = next((t for t in records if _same_club(t["to_club"], draft.to_club)
                      and t["commented_at"] == draft.commented_at), None)
    conflict = None
    if duplicate is None and records and draft.commented_at:
        first = records[0]
        if first["commented_at"] and draft.commented_at < first["commented_at"]:
            conflict = first
    ev = _check(window, draft.request(), exclude_id=conflict["id"] if conflict else None,
                user_ids=(draft.to_user,))
    notes = []
    if draft.to_user is None:
        notes.append(Issue("FA_NO_COACH", f"У клуба {draft.to_club} нет тренера в боте — ЛС не уйдёт."))
    if draft.commented_at is None:
        notes.append(Issue("FA_NO_DATE", "Время комментария неизвестно — перешлите комментарий, а не копию."))
    return FaPreview(draft, ev, conflict=conflict, duplicate=duplicate, notes=notes)


@dataclass
class FaRecorded:
    transfer: dict
    replaced: dict | None = None


def record_free_agent(draft: FaDraft, actor_id: int, *, replace_id: int | None = None) -> FaRecorded:
    """Записать СА одобренным. `replace_id` — отменить запись позднего комментария.

    Всё в одной транзакции: отмена, запись, справочник игроков.
    """
    with database.transaction():
        window = _window()
        replaced = None
        if replace_id is not None:
            replaced = repo.get_transfer(int(replace_id))
            if (replaced is None or replaced["kind"] != "free_agent" or replaced["window_id"] != window["id"]
                    or replaced["norm_name"] != norm_player(draft.player_name)
                    or replaced["status"] not in ACTIVE_STATUSES):
                raise InputError("Запись, которую нужно переписать, уже изменилась — перешлите комментарий заново.")
        records = _fa_records(window["id"], draft)
        if any(_same_club(t["to_club"], draft.to_club) and t["commented_at"] == draft.commented_at
               for t in records):
            raise InputError("Этот комментарий уже записан.")
        ev = _require(_check(window, draft.request(), exclude_id=replace_id, user_ids=(draft.to_user,)))
        if replaced is not None and not repo.set_transfer_status(
                replaced["id"], "cancelled", expected=ACTIVE_STATUSES,
                actor_id=int(actor_id), reason=FA_REASSIGN_REASON):
            raise InputError("Запись, которую нужно переписать, уже изменилась — перешлите комментарий заново.")
        tid = repo.insert_transfer(
            window["id"], "free_agent", draft.player_name, "pending_manager", _warnings(ev),
            from_club=draft.from_club, to_club=draft.to_club, to_user=draft.to_user,
            price_k=draft.price_k, ovr=draft.ovr, source_text=draft.source_text,
            commented_at=draft.commented_at, reported_budget_k=draft.reported_budget_k,
            photo_file_id=draft.photo_file_id, initiator_id=int(actor_id))
        repo.set_transfer_status(tid, "approved", expected=("pending_manager",), actor_id=int(actor_id))
        repo.upsert_player(draft.player_name, last_club=draft.to_club, ovr=draft.ovr, price_k=draft.price_k)
        return FaRecorded(repo.get_transfer(tid), repo.get_transfer(replaced["id"]) if replaced else None)


def edit_fa_draft(draft: FaDraft, text: str) -> FaDraft:
    """Исправленный ответственным текст → новый черновик с тем же временем и фото."""
    fresh = parse_fa_comment(text, commented_at=draft.commented_at, photo_file_id=draft.photo_file_id)
    return replace(fresh, source_text=draft.source_text if not text.strip() else fresh.source_text)


__all__ = [
    "URN_CLUB", "COUNTERPARTY_DECLINED", "FA_REASSIGN_REASON", "InputError", "coach_club",
    "create_deal", "create_surcharge", "create_urn_sale", "create_urn_buy", "counterparty_id",
    "confirm", "decline", "withdraw", "serialize", "my_status", "urn_items", "history", "photo_file_id",
    "looks_like_fa", "FaDraft", "commented_at_msk", "parse_fa_comment", "FaPreview", "fa_preview",
    "FaRecorded", "record_free_agent", "edit_fa_draft",
]
