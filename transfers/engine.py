"""Чистые расчёты и проверки трансферного окна: без БД, без Telegram, без сети.

Всё, что зависит от правил окна, приходит в `WindowSettings` (строка
`transfer_windows`), а не берётся из констант: правила меняются от окна к окну.

Деньги — целые тысячи (12.5 млн = 12500), чтобы не ловить ошибки дробей.

Проверка заявки (`evaluate_request`) делит находки на два вида:
* **блокировки** — заявку не принять (окно не в том статусе, санкция, OVR на
  потолке, правило «N игроков исходного состава», второй свободный агент,
  урна сверх лимита или для клуба из списка, неверные данные);
* **предупреждения** — заявка принимается, решает ответственный (бюджет,
  лимиты сделок, запретное имя, игрока нет в составе и т. п.).
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from club_registry import normalize_team_name
from services.player_names import normalize_player_name_key
from time_utils import parse_msk
from transfers import config as tcfg

ACTIVE_STATUSES = ("pending_counterparty", "pending_manager", "approved")
PENDING_STATUSES = ("pending_counterparty", "pending_manager")

# Какой стороне что стоит заявка. Покупатель — `to_club`, продавец — `from_club`.
BUY_SLOT_KINDS = ("deal", "free_agent", "urn_buy")
SELL_SLOT_KINDS = ("deal", "urn_sale")
SPEND_KINDS = ("deal", "free_agent", "surcharge", "urn_buy")
EARN_KINDS = ("deal", "urn_sale")


# ─── Деньги ──────────────────────────────────────────────────────────────────

def parse_money_k(value) -> int | None:
    """Сумма в млн (как её пишут люди) → целые тысячи.

    `12.5`, `"12,5"`, `"12.5 млн"`, `"10"` → 12500 / 12500 / 12500 / 10000.
    Отрицательное или нечитаемое → None. Точность — до тысячи.
    """
    if value is None or isinstance(value, bool):
        return None
    raw = str(value).strip().lower()
    raw = re.sub(r"\s*(млн|mln|m|м)\.?$", "", raw)
    raw = raw.replace(" ", "").replace(" ", "").replace(",", ".")
    # Только обычная запись: «1e400», «nan» и прочие формы Decimal людям не нужны.
    if not re.fullmatch(r"\d{1,9}(\.\d+)?", raw):
        return None
    try:
        amount = Decimal(raw)
    except InvalidOperation:
        return None
    return int((amount * 1000).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def format_k(amount_k: int | None) -> str:
    """Тысячи → «12.5 млн». Лишние нули не печатаются."""
    if amount_k is None:
        return "—"
    sign = "-" if amount_k < 0 else ""
    whole, rest = divmod(abs(int(amount_k)), 1000)
    if not rest:
        return f"{sign}{whole} млн"
    return f"{sign}{whole}.{rest:03d}".rstrip("0") + " млн"


def urn_payout(tm_price_k: int, special_price_k: int, sellable: bool,
               divisor_sellable: int = tcfg.DEFAULT_URN_DIVISOR_SELLABLE,
               divisor_unsellable: int = tcfg.DEFAULT_URN_DIVISOR_UNSELLABLE,
               step_k: int = tcfg.URN_ROUNDING_K) -> int:
    """Выплата за игрока в урне: (TM + спешл) / делитель, вниз до шага (0.1 млн)."""
    divisor = divisor_sellable if sellable else divisor_unsellable
    if divisor <= 0:
        raise ValueError("urn divisor must be positive")
    total = max(0, int(tm_price_k)) + max(0, int(special_price_k))
    payout = total // divisor
    return payout - payout % step_k if step_k > 0 else payout


def surcharge_cost(ovr: int | None, table: Mapping[int, int]) -> int | None:
    """Доплата за спешл по таблице OVR → тысячи. OVR вне таблицы → None."""
    if ovr is None:
        return None
    return table.get(int(ovr))


# ─── Имена ───────────────────────────────────────────────────────────────────

def norm_player(name: str | None) -> str:
    return normalize_player_name_key(name)


def norm_club(name: str | None) -> str:
    return normalize_team_name(name)


def name_covers(short: str | None, full: str | None) -> bool:
    """Короткое имя из состава («SAKA», «C. RONALDO») — часть полного («Bukayo Saka»).

    Каждое слово короткого имени есть в полном, однобуквенное — как инициал слова. Нужна хотя
    бы одна целая фамилия: «C.» само по себе не совпадает ни с кем.
    """
    words = sorted(norm_player(short).split(), key=len, reverse=True)
    rest = norm_player(full).split()
    if not words or len(words[0]) < 2:
        return False
    for w in words:
        hit = next((f for f in rest if f == w or (len(w) == 1 and f.startswith(w))), None)
        if hit is None:
            return False
        rest.remove(hit)
    return True


def is_full_latin_name(name: str | None) -> bool:
    """Имя латиницей и целиком: минимум два слова, только латинские буквы.

    Диакритика (é, ñ, ø, ı) допустима — это латиница. Дефис, апостроф и точка
    внутри имени тоже.
    """
    if not name:
        return False
    words = [w for w in re.split(r"\s+", name.strip()) if w]
    if len(words) < 2:
        return False
    letters = 0
    for ch in name:
        if ch.isspace() or ch in "-'’.":
            continue
        if not ch.isalpha():
            return False
        if not unicodedata.name(ch, "").startswith("LATIN"):
            return False
        letters += 1
    return letters >= 2


# ─── Настройки окна ──────────────────────────────────────────────────────────

def _json_list(value) -> tuple[str, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, (list, tuple)):
        items = value
    else:
        try:
            items = json.loads(value)
        except (TypeError, ValueError):
            return ()
    if not isinstance(items, list | tuple):
        return ()
    return tuple(str(x).strip() for x in items if str(x).strip())


def _json_table(value) -> dict[int, int] | None:
    if value is None or value == "":
        return None
    if isinstance(value, Mapping):
        items = value
    else:
        try:
            items = json.loads(value)
        except (TypeError, ValueError):
            return None
        if not isinstance(items, dict):
            return None
    table: dict[int, int] = {}
    for key, price in items.items():
        try:
            table[int(key)] = int(price)
        except (TypeError, ValueError):
            continue
    return table


@dataclass(frozen=True)
class WindowSettings:
    status: str = "draft"
    max_buys: int = tcfg.DEFAULT_MAX_BUYS
    max_sells: int = tcfg.DEFAULT_MAX_SELLS
    max_extra_slots: int = tcfg.DEFAULT_MAX_EXTRA_SLOTS
    slot_price_coins: int = tcfg.DEFAULT_SLOT_PRICE_COINS
    ovr_cap: int = tcfg.DEFAULT_OVR_CAP
    min_core_players: int = tcfg.DEFAULT_MIN_CORE_PLAYERS
    fa_opens_at: str | None = None
    fa_forbidden_clubs: tuple[str, ...] = ()
    fa_restricted_clubs: tuple[str, ...] = ()
    fa_ovr_cap: int = tcfg.DEFAULT_FA_OVR_CAP
    urn_divisor_sellable: int = tcfg.DEFAULT_URN_DIVISOR_SELLABLE
    urn_divisor_unsellable: int = tcfg.DEFAULT_URN_DIVISOR_UNSELLABLE
    urn_max_per_club: int = tcfg.DEFAULT_URN_MAX_PER_CLUB
    urn_restricted_clubs: tuple[str, ...] = ()
    surcharge_min_ovr: int = tcfg.DEFAULT_SURCHARGE_MIN_OVR
    surcharge_table: Mapping[int, int] = field(
        default_factory=lambda: dict(tcfg.DEFAULT_SURCHARGE_TABLE))

    @classmethod
    def from_row(cls, row: Mapping) -> WindowSettings:
        """Строка `transfer_windows` (sqlite3.Row или dict) → настройки.

        Пустая `surcharge_table` в окне значит «таблица по умолчанию».
        """
        get = row.get if isinstance(row, dict) else (lambda k, d=None: row[k] if k in row.keys() else d)
        table = _json_table(get("surcharge_table"))
        base = cls()
        return cls(
            status=get("status") or base.status,
            max_buys=_int(get("max_buys"), base.max_buys),
            max_sells=_int(get("max_sells"), base.max_sells),
            max_extra_slots=_int(get("max_extra_slots"), base.max_extra_slots),
            slot_price_coins=_int(get("slot_price_coins"), base.slot_price_coins),
            ovr_cap=_int(get("ovr_cap"), base.ovr_cap),
            min_core_players=_int(get("min_core_players"), base.min_core_players),
            fa_opens_at=get("fa_opens_at"),
            fa_forbidden_clubs=_json_list(get("fa_forbidden_clubs")),
            fa_restricted_clubs=_json_list(get("fa_restricted_clubs")),
            fa_ovr_cap=_int(get("fa_ovr_cap"), base.fa_ovr_cap),
            urn_divisor_sellable=_int(get("urn_divisor_sellable"), base.urn_divisor_sellable),
            urn_divisor_unsellable=_int(get("urn_divisor_unsellable"), base.urn_divisor_unsellable),
            urn_max_per_club=_int(get("urn_max_per_club"), base.urn_max_per_club),
            urn_restricted_clubs=_json_list(get("urn_restricted_clubs")),
            surcharge_min_ovr=_int(get("surcharge_min_ovr"), base.surcharge_min_ovr),
            surcharge_table=table if table is not None else dict(tcfg.DEFAULT_SURCHARGE_TABLE),
        )


def _int(value, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def club_in(clubs: Iterable[str], club: str | None) -> bool:
    """Клуб в списке настроек — сравнение по нормализованному имени."""
    key = norm_club(club)
    return bool(key) and any(norm_club(c) == key for c in clubs)


def _same_club(a: str | None, b: str | None) -> bool:
    ka, kb = norm_club(a), norm_club(b)
    return bool(ka) and ka == kb


# ─── Бюджет и слоты клуба ────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClubLedger:
    """Бюджет и слоты клуба в окне, посчитанные из заявок — не хранятся.

    Траты и слоты считаются по заявкам `pending_*` и `approved`, чтобы очередь
    нельзя было завалить сверх лимита. Доход от продажи — только по одобренной:
    на ещё не подтверждённые деньги покупать нельзя.
    """
    club: str
    budget_k: int = 0
    spent_k: int = 0
    earned_k: int = 0
    buys_used: int = 0
    sells_used: int = 0
    urn_sales: int = 0
    extra_buys: int = 0
    extra_sells: int = 0
    max_buys: int = tcfg.DEFAULT_MAX_BUYS
    max_sells: int = tcfg.DEFAULT_MAX_SELLS

    @property
    def remaining_k(self) -> int:
        return self.budget_k + self.earned_k - self.spent_k

    @property
    def buys_limit(self) -> int:
        return self.max_buys + self.extra_buys

    @property
    def sells_limit(self) -> int:
        return self.max_sells + self.extra_sells

    @property
    def buys_left(self) -> int:
        return max(0, self.buys_limit - self.buys_used)

    @property
    def sells_left(self) -> int:
        return max(0, self.sells_limit - self.sells_used)


def compute_ledger(club: str, budget_k: int | None, transfers: Iterable[Mapping],
                   settings: WindowSettings,
                   slot_purchases: Iterable[Mapping] = (),
                   exclude_transfer_id: int | None = None) -> ClubLedger:
    """Бюджет и слоты `club` по заявкам окна.

    `budget_k=None` — у клуба нет строки бюджета: считается 0, и заявка получит
    обычное предупреждение о превышении. `exclude_transfer_id` убирает из
    расчёта саму проверяемую заявку при повторной проверке.
    """
    spent = earned = buys = sells = urns = 0
    for t in transfers:
        if t["status"] not in ACTIVE_STATUSES:
            continue
        if exclude_transfer_id is not None and t["id"] == exclude_transfer_id:
            continue
        kind = t["kind"]
        price = int(t["price_k"] or 0)
        discount = int(t.get("discount_k") or 0)
        buyer_price = max(0, price - discount)
        if _same_club(t["to_club"], club):
            if kind in SPEND_KINDS:
                spent += buyer_price
            if kind in BUY_SLOT_KINDS:
                buys += 1
        if _same_club(t["from_club"], club):
            if kind in SELL_SLOT_KINDS:
                sells += 1
            if kind == "urn_sale":
                urns += 1
            if kind in EARN_KINDS and t["status"] == "approved":
                earned += price

    extra_buys = extra_sells = 0
    for p in slot_purchases:
        if p["status"] != "active" or not _same_club(p["club_name"], club):
            continue
        if p["slot_type"] == "buy":
            extra_buys += 1
        elif p["slot_type"] == "sell":
            extra_sells += 1

    return ClubLedger(
        club=club, budget_k=int(budget_k or 0), spent_k=spent, earned_k=earned,
        buys_used=buys, sells_used=sells, urn_sales=urns,
        extra_buys=extra_buys, extra_sells=extra_sells,
        max_buys=settings.max_buys, max_sells=settings.max_sells,
    )


def core_remaining(snapshot: Iterable[str], squad: Iterable[str], outgoing: Iterable[str]) -> int:
    """Сколько игроков исходного состава останется в клубе.

    `snapshot` — состав на открытие окна, `squad` — текущий, `outgoing` — игроки
    в активных заявках на уход (включая проверяемую). Имена любые: сравниваются
    по `normalize_player_name_key`.
    """
    core = {norm_player(n) for n in snapshot} - {""}
    present = {norm_player(n) for n in squad}
    leaving = {norm_player(n) for n in outgoing}
    return len((core & present) - leaving)


# ─── Проверка заявки ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Issue:
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclass
class Evaluation:
    price_k: int | None = None
    blocks: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocks

    def block(self, code: str, message: str) -> None:
        self.blocks.append(Issue(code, message))

    def warn(self, code: str, message: str) -> None:
        self.warnings.append(Issue(code, message))

    def warnings_json(self) -> str:
        return json.dumps([w.as_dict() for w in self.warnings], ensure_ascii=False)


@dataclass(frozen=True)
class TransferRequest:
    kind: str
    player_name: str
    from_club: str | None = None
    to_club: str | None = None
    price_k: int | None = None
    ovr: int | None = None
    tm_price_k: int | None = None
    special_price_k: int | None = None
    discount_k: int = 0
    sellable: bool = True
    commented_at: str | None = None
    reported_budget_k: int | None = None


@dataclass(frozen=True)
class RequestContext:
    """Всё, что проверке нужно знать из базы. Собирает сервис.

    Леджеры считаются **без** самой проверяемой заявки.
    """
    settings: WindowSettings
    buyer: ClubLedger | None = None
    seller: ClubLedger | None = None
    directory: Mapping | None = None          # строка transfer_players по игроку
    sanctioned: bool = False                  # клуб или тренер под санкцией
    user_free_agents: int = 0                 # активные СА этого тренера в окне
    fa_taken_by: Mapping | None = None        # активный СА на этого игрока у другого клуба
    seller_core_after: int | None = None      # ядро продавца, если заявка пройдёт
    player_in_seller_squad: bool | None = None
    urn_item: Mapping | None = None           # для urn_buy: запись urn_sale
    urn_item_taken: bool = False              # на игрока из урны уже есть активный выкуп


_KIND_WINDOW = {
    # статус окна → какие заявки он принимает
    "draft": ("urn_sale",),
    "open": ("deal", "free_agent", "surcharge", "urn_sale", "urn_buy"),
    "closed": (),
}


def evaluate_request(req: TransferRequest, ctx: RequestContext) -> Evaluation:
    """Проверить заявку до записи. Чистая функция."""
    s = ctx.settings
    ev = Evaluation(price_k=req.price_k)

    if req.kind not in BUY_SLOT_KINDS + SELL_SLOT_KINDS + ("surcharge",):
        ev.block("INVALID_KIND", "Неизвестный тип заявки.")
        return ev
    if req.kind not in _KIND_WINDOW.get(s.status, ()):
        if s.status == "draft":
            ev.block("WINDOW_DRAFT", "Окно ещё не открыто: сейчас принимается только урна.")
        else:
            ev.block("WINDOW_CLOSED", "Трансферное окно закрыто.")
        return ev
    if not norm_player(req.player_name):
        ev.block("INVALID_PLAYER", "Не указан игрок.")
        return ev
    if ctx.sanctioned:
        ev.block("SANCTIONED", "Клуб или тренер лишён трансферного окна.")
    if ctx.directory is not None and ctx.directory["banned"]:
        reason = ctx.directory["ban_reason"] or "в списке запретных"
        ev.warn("BANNED_PLAYER", f"Игрок в списке запретных: {reason}.")

    handler = {
        "deal": _check_deal,
        "free_agent": _check_free_agent,
        "surcharge": _check_surcharge,
        "urn_sale": _check_urn_sale,
        "urn_buy": _check_urn_buy,
    }[req.kind]
    handler(req, ctx, ev)
    return ev


def _check_ovr_cap(ovr: int | None, cap: int, ev: Evaluation) -> None:
    if ovr is not None and ovr >= cap:
        ev.block("OVR_CAP", f"OVR {ovr} — карты с OVR {cap}+ запрещены.")


def _check_buyer(ctx: RequestContext, price_k: int | None, ev: Evaluation, *, slot: bool) -> None:
    buyer = ctx.buyer
    if buyer is None:
        return
    if price_k is not None and price_k > buyer.remaining_k:
        ev.warn("BUDGET_EXCEEDED",
                f"Бюджет превышен: цена {format_k(price_k)}, остаток {format_k(buyer.remaining_k)}.")
    if slot and buyer.buys_left <= 0:
        ev.warn("BUY_LIMIT", f"Лимит покупок исчерпан ({buyer.buys_used} из {buyer.buys_limit}).")


def _check_seller(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    seller = ctx.seller
    if seller is not None and seller.sells_left <= 0:
        ev.warn("SELL_LIMIT", f"Лимит продаж исчерпан ({seller.sells_used} из {seller.sells_limit}).")
    if ctx.player_in_seller_squad is False:
        ev.warn("NOT_IN_SQUAD", f"{req.player_name} нет в составе {req.from_club}.")
    if ctx.seller_core_after is not None and ctx.seller_core_after < ctx.settings.min_core_players:
        ev.block("CORE_RULE",
                 f"В {req.from_club} останется {ctx.seller_core_after} игроков исходного состава, "
                 f"нужно минимум {ctx.settings.min_core_players}.")


def _check_deal(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    if not norm_club(req.from_club) or not norm_club(req.to_club):
        ev.block("INVALID_CLUBS", "Нужны оба клуба: откуда и куда.")
        return
    if _same_club(req.from_club, req.to_club):
        ev.block("INVALID_CLUBS", "Клуб не может продать игрока сам себе.")
        return
    if req.price_k is None or req.price_k < 0:
        ev.block("INVALID_PRICE", "Не указана цена.")
        return
    if req.ovr is None:
        ev.block("INVALID_OVR", "Не указан OVR карты.")
        return
    _check_ovr_cap(req.ovr, ctx.settings.ovr_cap, ev)
    buyer_price = max(0, req.price_k - req.discount_k) if req.discount_k else req.price_k
    _check_buyer(ctx, buyer_price, ev, slot=True)
    _check_seller(req, ctx, ev)


def _check_free_agent(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    s = ctx.settings
    if not norm_club(req.to_club):
        ev.block("INVALID_CLUBS", "Не указан клуб, куда переходит игрок.")
        return
    if req.price_k is None or req.price_k < 0:
        ev.block("INVALID_PRICE", "Не указана цена.")
        return
    if ctx.user_free_agents >= 1:
        ev.block("FA_SECOND", "У тренера уже есть свободный агент в этом окне.")

    opens = parse_msk(s.fa_opens_at)
    commented = parse_msk(req.commented_at)
    if opens is not None and commented is not None and commented < opens:
        ev.warn("FA_TOO_EARLY",
                f"Комментарий написан раньше старта СА ({opens:%d.%m %H:%M} МСК).")
    if club_in(s.fa_forbidden_clubs, req.from_club):
        ev.warn("FA_FORBIDDEN_CLUB", f"Из клуба {req.from_club} свободных агентов брать нельзя.")
    if club_in(s.fa_restricted_clubs, req.to_club):
        ev.warn("FA_RESTRICTED_CLUB", f"{req.to_club} не может брать свободных агентов.")
    if req.ovr is not None and req.ovr >= s.fa_ovr_cap:
        ev.warn("FA_OVR_CAP", f"OVR {req.ovr} — для СА запрещены карты {s.fa_ovr_cap}+.")
    if not is_full_latin_name(req.player_name):
        ev.warn("FA_NAME_FORMAT", "Имя должно быть латиницей и целиком (имя и фамилия).")
    if ctx.fa_taken_by is not None:
        taken = ctx.fa_taken_by
        ev.warn("FA_TAKEN",
                f"Игрок уже записан за {taken['to_club']} (комментарий {taken['commented_at'] or '—'}).")
    if req.reported_budget_k is not None and ctx.buyer is not None:
        expected = ctx.buyer.remaining_k - req.price_k
        if req.reported_budget_k != expected:
            ev.warn("FA_BUDGET_MISMATCH",
                    f"Указан остаток {format_k(req.reported_budget_k)}, по расчёту {format_k(expected)}.")
    _check_buyer(ctx, req.price_k, ev, slot=True)


def _check_surcharge(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    s = ctx.settings
    if not norm_club(req.to_club):
        ev.block("INVALID_CLUBS", "Не указан клуб.")
        return
    if req.ovr is None:
        ev.block("INVALID_OVR", "Не указан новый OVR.")
        return
    if req.ovr < s.surcharge_min_ovr:
        ev.block("SURCHARGE_MIN_OVR", f"Доплата берётся за OVR от {s.surcharge_min_ovr}.")
        return
    _check_ovr_cap(req.ovr, s.ovr_cap, ev)

    previous = ctx.directory["ovr"] if ctx.directory is not None else None
    if previous is None:
        ev.warn("SURCHARGE_NO_HISTORY", "Нет данных о прежней карте игрока — сверьте OVR вручную.")
    elif req.ovr <= previous:
        ev.block("SURCHARGE_NOT_HIGHER", f"Новый OVR {req.ovr} не выше прежнего ({previous}).")

    cost = surcharge_cost(req.ovr, s.surcharge_table)
    if cost is None:
        ev.block("SURCHARGE_NO_PRICE", f"Для OVR {req.ovr} нет цены в таблице доплат.")
        return
    ev.price_k = cost
    _check_buyer(ctx, cost, ev, slot=False)


def _check_urn_sale(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    s = ctx.settings
    if not norm_club(req.from_club):
        ev.block("INVALID_CLUBS", "Не указан клуб.")
        return
    if req.tm_price_k is None or req.special_price_k is None \
            or req.tm_price_k < 0 or req.special_price_k < 0:
        ev.block("INVALID_PRICE", "Нужны цена по Transfermarkt и цена за спешл.")
        return
    if club_in(s.urn_restricted_clubs, req.from_club):
        ev.block("URN_RESTRICTED", f"{req.from_club} не может пользоваться урной.")
    if ctx.seller is not None and ctx.seller.urn_sales >= s.urn_max_per_club:
        ev.block("URN_LIMIT", f"Урна доступна {s.urn_max_per_club} раз(а) за окно.")
    if ctx.directory is not None and ctx.directory["ovr"] is not None:
        ev.warn("URN_KNOWN_PLAYER",
                f"Игрок уже был в сделках (OVR {ctx.directory['ovr']}) — проверьте, что он не ходовой.")
    ev.price_k = urn_payout(req.tm_price_k, req.special_price_k, req.sellable,
                            s.urn_divisor_sellable, s.urn_divisor_unsellable)
    _check_seller(req, ctx, ev)


def _check_urn_buy(req: TransferRequest, ctx: RequestContext, ev: Evaluation) -> None:
    s = ctx.settings
    if not norm_club(req.to_club):
        ev.block("INVALID_CLUBS", "Не указан клуб.")
        return
    item = ctx.urn_item
    if item is None or item["kind"] != "urn_sale" or item["status"] != "approved":
        ev.block("URN_ITEM_MISSING", "Этого игрока нет в урне.")
        return
    if ctx.urn_item_taken:
        ev.block("URN_ITEM_TAKEN", "Игрока из урны уже выкупают.")
    if club_in(s.urn_restricted_clubs, req.to_club):
        ev.block("URN_RESTRICTED", f"{req.to_club} не может выкупать из урны.")
    _check_ovr_cap(req.ovr, s.ovr_cap, ev)
    if _same_club(item["from_club"], req.to_club):
        ev.warn("URN_OWN_BUYBACK", "Клуб выкупает своего же игрока из урны.")
    price = int(item["tm_price_k"] or 0) + int(item["special_price_k"] or 0)
    ev.price_k = price
    buyer_price = max(0, price - req.discount_k) if req.discount_k else price
    _check_buyer(ctx, buyer_price, ev, slot=True)
