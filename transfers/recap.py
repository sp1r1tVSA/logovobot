"""Итоги окна: цифры по одобренным заявкам, картинка и подпись для ленты.

Считаются только `approved`. Обмен — две связанные заявки, но один обмен: для счётчика обменов и
активности клуба пара идёт за одно событие. Оборот — сумма цен одобренных заявок без выплат из
урны (`urn_sale`: это деньги тренеру от урны, а не сделка между клубами). В топ идут заявки,
где клуб платит за игрока: сделка (в том числе половина обмена), выкуп из урны, свободный агент.
Доплата за спецкарту — не сделка и в топ не попадает, но в оборот входит.
"""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import dataclass, field

from transfers import repo
from transfers.engine import format_k, norm_club

logger = logging.getLogger(__name__)

TOP_LIMIT = 5
TOP_KINDS = ("deal", "urn_buy", "free_agent")
URN = "Урна"


@dataclass
class Recap:
    window_id: int
    title: str = ""
    requests_count: int = 0
    turnover_k: int = 0
    swaps: int = 0
    urn_sales: int = 0
    top_club: str | None = None
    top_club_count: int = 0
    top_deals: list[dict] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return self.requests_count == 0


def _route(t: dict) -> str:
    kind = t.get("kind")
    if kind == "urn_sale":
        return f"{t.get('from_club') or '—'} → {URN}"
    if kind == "urn_buy":
        return f"{URN} → {t.get('to_club') or '—'}"
    return " → ".join(c for c in (t.get("from_club"), t.get("to_club")) if c)


def _event_id(t: dict) -> int:
    """Обмен из двух заявок — одно событие, номер берём меньший."""
    partner = t.get("swap_partner_id")
    return min(t["id"], partner) if partner else t["id"]


def build(window_id: int) -> Recap:
    window = repo.get_window(window_id) or {}
    approved = repo.list_transfers(window_id, statuses=("approved",))
    recap = Recap(window_id=window_id, title=(window.get("title") or "").strip())

    events: set[int] = set()
    swap_events: set[int] = set()
    clubs: dict[str, tuple[str, set[int]]] = {}
    for t in approved:
        event = _event_id(t)
        events.add(event)
        if t.get("swap_partner_id"):
            swap_events.add(event)
        if t["kind"] == "urn_sale":
            recap.urn_sales += 1
        else:
            recap.turnover_k += int(t.get("price_k") or 0)
        for club in {t.get("from_club"), t.get("to_club")}:
            key = norm_club(club) if club else ""
            if key:
                clubs.setdefault(key, (club, set()))[1].add(event)
    recap.requests_count = len(events)
    recap.swaps = len(swap_events)

    if clubs:
        # больше событий — раньше; при равенстве по алфавиту, чтобы результат не плавал
        name, seen = min(clubs.values(), key=lambda v: (-len(v[1]), norm_club(v[0])))
        recap.top_club, recap.top_club_count = name, len(seen)

    deals = [t for t in approved if t["kind"] in TOP_KINDS and t.get("price_k")]
    deals.sort(key=lambda t: (-int(t["price_k"]), t["id"]))
    recap.top_deals = deals[:TOP_LIMIT]
    return recap


def caption(recap: Recap, window_title: str) -> str:
    """Подпись под картинкой / запасной текст, если картинку отправить не вышло."""
    lines = [f"🏁 <b>Итоги окна {window_title}</b>", ""]
    if recap.empty:
        lines.append("Одобренных заявок не было.")
        return "\n".join(lines)
    lines.append(f"Оборот: <b>{format_k(recap.turnover_k)}</b>")
    parts = [f"заявок {recap.requests_count}"]
    if recap.swaps:
        parts.append(f"обменов {recap.swaps}")
    if recap.urn_sales:
        parts.append(f"продаж в урну {recap.urn_sales}")
    lines.append(", ".join(parts).capitalize())
    if recap.top_club:
        lines.append(f"Самый активный клуб: <b>{html.escape(recap.top_club)}</b> ({recap.top_club_count})")
    if recap.top_deals:
        lines += ["", "<b>Топ сделок:</b>"]
        for i, t in enumerate(recap.top_deals, 1):
            lines.append(f"{i}. {html.escape(t['player_name'])} — {html.escape(_route(t))}, "
                         f"{format_k(t['price_k'])}")
    return "\n".join(lines)


def build_image(recap: Recap) -> bytes | None:
    """PNG итогов или `None`. Блокирует — зовите в потоке. Наружу не бросает."""
    try:
        from services.graphics.transfer_recap_generator import RecapDeal, render_window_recap

        return render_window_recap(
            title=recap.title or f"Окно №{recap.window_id}",
            turnover_text=format_k(recap.turnover_k),
            deals=[RecapDeal(t["player_name"], _route(t), format_k(t["price_k"])) for t in recap.top_deals],
            requests_count=recap.requests_count,
            swaps=recap.swaps,
            urn_sales=recap.urn_sales,
            top_club=recap.top_club,
            top_club_count=recap.top_club_count,
        )
    except Exception:
        logger.exception("transfers: recap render failed for window %s", recap.window_id)
        return None


async def build_image_async(recap: Recap) -> bytes | None:
    return await asyncio.to_thread(build_image, recap)
