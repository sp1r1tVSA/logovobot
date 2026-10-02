"""Жизненный цикл трансферного окна: открытие, закрытие, автозакрытие,
снимок исходного состава и бюджеты клубов.

Без Telegram: хендлеры, джоб автозакрытия и (позже) API зовут эти функции и
сами решают, кому что отправить. Ошибки ввода человека — `InputError` с
готовым русским текстом.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

import config
import database
from club_registry import resolve_club_query, resolve_team_name
from time_utils import DT_FORMAT, now_msk, parse_msk
from transfers import repo
from transfers.engine import format_k, norm_club, norm_player, parse_money_k

# Действия бота от своего имени (автозакрытие, автоотклонение) пишутся как actor 0.
SYSTEM_ACTOR = 0
AUTO_REJECT_REASON = "окно закрыто"

# Списки клубов лиги — их имена приводятся к каноническим. В `fa_forbidden_clubs`
# лежат реальные клубы, откуда приходят свободные агенты, их пишем как есть.
LEAGUE_CLUB_LISTS = ("fa_restricted_clubs", "urn_restricted_clubs")
_CLEAR_WORDS = ("off", "нет", "-", "—", "выкл", "none")


class InputError(ValueError):
    """Ввод ответственного не разобран; текст исключения — для показа ему."""


def is_transfer_manager(user_id: int | None) -> bool:
    """Ответственный за ТО — только `TRANSFER_MANAGER_ID`, без замен и исключений.

    Значение читается при каждом вызове, как `ADMIN_IDS` в `handlers/base.py`.
    """
    manager = getattr(config, "TRANSFER_MANAGER_ID", None)
    return manager is not None and user_id is not None and int(user_id) == int(manager)


def can_manage_window(user_id: int | None) -> bool:
    """Панель `/to`: ответственный за ТО и админы из `ADMIN_IDS` (только env, без ролей в БД).

    Уведомления «ответственному» по-прежнему идут одному `TRANSFER_MANAGER_ID`.
    """
    if user_id is None:
        return False
    return is_transfer_manager(user_id) or int(user_id) in config.ADMIN_IDS


# ─── Дата и время ────────────────────────────────────────────────────────────

_DT_INPUT_FORMATS = (
    "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S",
    "%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%y %H:%M",
)


def parse_window_datetime(text: str | None, *, now: dt.datetime | None = None) -> dt.datetime | None:
    """Время, как его пишет человек, по Москве → naive datetime.

    `2026-10-10 20:00`, `10.10.2026 20:00`, `10.10 20:00`. Без года берётся
    ближайший будущий: «05.01» в декабре — это январь следующего года.
    Нечитаемое → None.
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return None
    for fmt in _DT_INPUT_FORMATS:
        try:
            return dt.datetime.strptime(raw, fmt)
        except ValueError:
            continue
    try:
        partial = dt.datetime.strptime(raw, "%d.%m %H:%M")
    except ValueError:
        return None
    current = now or now_msk()
    try:
        moment = partial.replace(year=current.year)
        if moment < current:
            moment = partial.replace(year=current.year + 1)
    except ValueError:      # 29.02 не в високосный год
        return None
    return moment


def _is_clear(text: str) -> bool:
    return text.strip().lower() in _CLEAR_WORDS


# ─── Окно ────────────────────────────────────────────────────────────────────

def create_window(actor_id: int, title: str | None = None) -> int:
    """Черновик окна на активный сезон. Есть незакрытое окно — `InputError`."""
    season = database.get_active_season()
    try:
        return repo.create_window(int(season) if season else None, actor_id, title)
    except repo.WindowConflict as exc:
        raise InputError("Уже есть незакрытое окно — сначала закройте его.") from exc


@dataclass
class SnapshotResult:
    clubs_total: int = 0                      # клубов лиги
    clubs_saved: int = 0                      # клубов, записанных этим прогоном
    players_added: int = 0
    without_squad: list[str] = field(default_factory=list)   # клубы лиги без состава


def league_clubs() -> list[str]:
    """Клубы всех активных дивизионов, канонические имена, без повторов."""
    seen: dict[str, str] = {}
    for division in database.get_divisions(is_active=True):
        for club in database.get_division_teams(division["id"]):
            key = norm_club(club)
            if key and key not in seen:
                seen[key] = club
    return sorted(seen.values())


def _squads_by_club() -> dict[str, dict[str, tuple[str, str | None]]]:
    """{клуб: {игрок: (имя, позиция)}} по текущим `squad_players`."""
    squads: dict[str, dict[str, tuple[str, str | None]]] = {}
    for row in repo.list_squad_players():
        team = row["team_name"] or ""
        club_key = norm_club(resolve_team_name(team) or team)
        player_key = norm_player(row["player_name"])
        if club_key and player_key:
            squads.setdefault(club_key, {}).setdefault(
                player_key, (row["player_name"].strip(), row["position"]))
    return squads


def _undo_window_ops(window_id: int, squads: dict) -> None:
    """Откатить в `squads` изменения состава, уже сделанные этим окном.

    Снимок — это состав на открытие. Если клуб без состава дозаписывают позже,
    купленные в окне игроки ядром не становятся, а проданные в него входят.
    """
    for op in reversed(repo.list_window_squad_ops(window_id)):
        club_key = norm_club(resolve_team_name(op["team_name"]) or op["team_name"])
        player_key = norm_player(op["player_name"])
        roster = squads.setdefault(club_key, {})
        if op["op"] == "add":
            roster.pop(player_key, None)
        else:
            roster.setdefault(player_key, (op["player_name"].strip(), op["position"]))


def snapshot_core(window_id: int, *, only_missing: bool = True) -> SnapshotResult:
    """Записать исходный состав клубов лиги — основу правила «N игроков ядра».

    Повторный прогон дописывает только клубы, у которых снимка ещё нет (их
    состав могли загрузить после открытия). Уже записанного не трогает.
    """
    result = SnapshotResult()
    with database.transaction():
        clubs = league_clubs()
        squads = _squads_by_club()
        _undo_window_ops(window_id, squads)
        done = repo.core_snapshot_clubs(window_id) if only_missing else set()
        result.clubs_total = len(clubs)
        for club in clubs:
            key = norm_club(club)
            roster = squads.get(key)
            if not roster:
                if key not in done:
                    result.without_squad.append(club)
                continue
            if key in done:
                continue
            added = repo.save_core_snapshot(window_id, club, list(roster.values()))
            if added:
                result.clubs_saved += 1
                result.players_added += added
    return result


@dataclass
class OpenResult:
    opened: bool
    snapshot: SnapshotResult | None = None


def open_window(window_id: int, actor_id: int) -> OpenResult:
    """`draft` → `open` и снимок состава — одной транзакцией."""
    with database.transaction():
        if not repo.open_window(window_id, actor_id):
            return OpenResult(False)
        return OpenResult(True, snapshot_core(window_id))


@dataclass
class CloseResult:
    closed: bool
    rejected: list[dict] = field(default_factory=list)   # автоотклонённые заявки


def close_window(window_id: int, actor_id: int) -> CloseResult:
    """Закрыть окно и отклонить заявки, которые вторая сторона не подтвердила.

    `pending_manager` остаются: их ответственный решает и после закрытия.
    """
    with database.transaction():
        if not repo.close_window(window_id, actor_id):
            return CloseResult(False)
        rejected = []
        for t in repo.list_transfers(window_id, statuses=("pending_counterparty",)):
            if repo.set_transfer_status(t["id"], "rejected", expected=("pending_counterparty",),
                                        actor_id=SYSTEM_ACTOR, reason=AUTO_REJECT_REASON):
                rejected.append(repo.get_transfer(t["id"]))
        return CloseResult(True, rejected)


def due_auto_close(now: dt.datetime | None = None) -> dict | None:
    """Открытое окно, чьё время автозакрытия наступило. Черновик сам не закрывается."""
    window = repo.get_active_window()
    if not window or window["status"] != "open":
        return None
    moment = parse_msk(window["auto_close_at"])
    if moment is None or moment > (now or now_msk()):
        return None
    return window


def set_auto_close(window_id: int, text: str, *, now: dt.datetime | None = None) -> dict:
    """Задать или снять (`off`) время автозакрытия. Прошедшее время — `InputError`."""
    if _is_clear(text):
        return repo.update_window_settings(window_id, {"auto_close_at": None})
    moment = parse_window_datetime(text, now=now)
    if moment is None:
        raise InputError("Не понял время. Пример: <code>10.10 20:00</code> или <code>2026-10-10 20:00</code>.")
    if moment <= (now or now_msk()):
        raise InputError("Это время уже прошло.")
    return repo.update_window_settings(window_id, {"auto_close_at": moment.strftime(DT_FORMAT)})


def _parse_setting(key: str, text: str):
    """Текст ответственного → значение для `repo.update_window_settings`."""
    raw = text.strip()
    if key in repo.INT_SETTINGS:
        if not raw.isdigit():
            raise InputError("Нужно целое неотрицательное число.")
        return int(raw)
    if key in repo.LIST_SETTINGS:
        if _is_clear(raw):
            return []
        names = [part.strip() for part in raw.split(",") if part.strip()]
        if key not in LEAGUE_CLUB_LISTS:
            return names
        clubs, unknown = [], []
        for name in names:
            found = resolve_club_query(name)
            if found.canonical:
                clubs.append(found.canonical)
            else:
                unknown.append(name)
        if unknown:
            raise InputError("Не нашёл клубы: " + ", ".join(unknown))
        return clubs
    if key in repo.TABLE_SETTINGS:
        if _is_clear(raw):
            return None
        table = {}
        # Разделитель — запятая/точка с запятой перед «OVR=», чтобы «12,5» осталось суммой.
        for part in re.split(r"[;,]\s*(?=\d+\s*=)", raw):
            ovr, sep, price = part.partition("=")
            amount = parse_money_k(price)
            if not sep or not ovr.strip().isdigit() or amount is None:
                raise InputError("Формат таблицы: <code>100=10, 111=160</code> (OVR=млн).")
            table[int(ovr.strip())] = amount
        return table
    if key in repo.DATETIME_SETTINGS:
        if _is_clear(raw):
            return None
        moment = parse_window_datetime(raw)
        if moment is None:
            raise InputError("Не понял время. Пример: <code>10.10 20:00</code>.")
        return moment.strftime(DT_FORMAT)
    return None if _is_clear(raw) else raw          # title


def update_setting(window_id: int, key: str, text: str) -> dict:
    """Поменять одну настройку окна из текста. Автозакрытие — через `set_auto_close`."""
    if key == "auto_close_at":
        return set_auto_close(window_id, text)
    if key not in repo.SETTINGS_KEYS:
        raise InputError("Нет такой настройки.")
    value = _parse_setting(key, text)
    try:
        return repo.update_window_settings(window_id, {key: value})
    except ValueError as exc:
        raise InputError("Значение вне допустимых границ.") from exc


# ─── Бюджеты ─────────────────────────────────────────────────────────────────

def default_budgets(window_id: int) -> dict[str, int]:
    """Бюджеты клубов по правилам окна: {клуб: тысячи}.

    Единственное место, куда подключаются правила выдачи бюджетов. Владелец
    пришлёт их до начала окна; пока правил нет, функция ничего не выдаёт, и
    бюджеты вносит ответственный руками (`set_budget`). Клуб без бюджета
    считается с нулём и получает обычное предупреждение «бюджет превышен».
    """
    return {}


@dataclass
class BudgetsApplied:
    written: int = 0
    kept_manual: list[str] = field(default_factory=list)   # ручные строки, которые не тронули
    invalid: list[str] = field(default_factory=list)       # клуб не из лиги или плохая сумма


def apply_default_budgets(window_id: int, actor_id: int, *, overwrite_manual: bool = False,
                          rules: dict[str, int] | None = None) -> BudgetsApplied:
    """Записать бюджеты по правилам (`source='rule'`). Ручные правки по умолчанию важнее."""
    result = BudgetsApplied()
    budgets = default_budgets(window_id) if rules is None else rules
    with database.transaction():
        existing = {norm_club(b["club_name"]): b for b in repo.get_club_budgets(window_id)}
        for club, amount in budgets.items():
            found = resolve_club_query(club).canonical
            if not found or isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
                result.invalid.append(str(club))
                continue
            row = existing.get(norm_club(found))
            if row is not None and row["source"] == "manual" and not overwrite_manual:
                result.kept_manual.append(found)
                continue
            repo.set_club_budget(window_id, found, amount, actor_id, source="rule")
            result.written += 1
    return result


def resolve_club(text: str) -> str:
    """Клуб лиги по тексту человека; неоднозначность или промах — `InputError`."""
    found = resolve_club_query(text)
    if found.canonical:
        return found.canonical
    if found.suggestions:
        raise InputError("Уточните клуб: " + ", ".join(found.suggestions))
    raise InputError(f"Клуб «{text.strip()}» не найден.")


def set_budget(window_id: int, club_text: str, amount_text: str, actor_id: int) -> tuple[str, int, int | None]:
    """Ручной бюджет клуба. → (клуб, новый бюджет, прежний или None)."""
    club = resolve_club(club_text)
    amount = parse_money_k(amount_text)
    if amount is None:
        raise InputError("Не понял сумму. Пример: <code>120</code> или <code>12,5</code> (в млн).")
    with database.transaction():
        old = repo.get_club_budget(window_id, club)
        repo.set_club_budget(window_id, club, amount, actor_id, source="manual")
    return club, amount, old


def _budget_row(club: str, budget: dict | None) -> dict:
    return {"club": club, "budget_k": budget["budget_k"] if budget else None,
            "source": budget["source"] if budget else None}


def budget_table(window_id: int) -> list[dict]:
    """Все клубы лиги с бюджетом (None — не задан), по алфавиту."""
    budgets = {norm_club(b["club_name"]): b for b in repo.get_club_budgets(window_id)}
    rows = [_budget_row(club, budgets.pop(norm_club(club), None)) for club in league_clubs()]
    rows += [_budget_row(b["club_name"], b) for b in budgets.values()]   # клуб, которого уже нет в лиге
    return sorted(rows, key=lambda r: norm_club(r["club"]))


OUTSIDE_LEAGUE = "Вне лиги"


def budget_pages(window_id: int) -> list[tuple[str, list[dict]]]:
    """Бюджеты по дивизионам для панели: [(дивизион, строки по алфавиту)].

    Клуб с бюджетом, которого в лиге уже нет, попадает на последнюю страницу
    «Вне лиги». Порядок дивизионов — как в `get_divisions`.
    """
    budgets = {norm_club(b["club_name"]): b for b in repo.get_club_budgets(window_id)}
    pages: list[tuple[str, list[dict]]] = []
    seen: set[str] = set()
    for division in database.get_divisions(is_active=True):
        rows = []
        for club in database.get_division_teams(division["id"]):
            key = norm_club(club)
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(_budget_row(club, budgets.pop(key, None)))
        if rows:
            pages.append((division["name"], sorted(rows, key=lambda r: norm_club(r["club"]))))
    if budgets:
        pages.append((OUTSIDE_LEAGUE, sorted((_budget_row(b["club_name"], b) for b in budgets.values()),
                                             key=lambda r: norm_club(r["club"]))))
    return pages


# ─── Темы группы ─────────────────────────────────────────────────────────────

# Ссылка на тему (⋯ → «Копировать ссылку») или на сообщение в ней:
#   t.me/c/<id>/<тема>  ·  t.me/c/<id>/<тема>/<сообщение>  ·  t.me/c/<id>/<сообщение>?thread=<тема>
#   и то же с @username публичной группы вместо c/<id>.
_TOPIC_LINK = re.compile(
    r"^(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/"
    r"(?:c/(?P<cid>\d+)|(?P<user>[A-Za-z][A-Za-z0-9_]{3,31}))"
    r"/(?P<first>\d+)(?:/(?P<second>\d+))?/?(?:\?(?P<query>\S*))?$")


def parse_topic_link(text: str | None) -> tuple[int | str, int]:
    """Ссылка на тему форума → (chat_id или «@username», id темы)."""
    match = _TOPIC_LINK.match((text or "").strip())
    if not match:
        raise InputError("Это не ссылка на тему. В теме нажмите ⋯ → «Копировать ссылку» "
                         "и пришлите её сюда.")
    thread = int(match["first"])
    for part in (match["query"] or "").split("&"):
        key, _, value = part.partition("=")
        if key in ("thread", "topic") and value.isdigit():
            thread = int(value)
    if thread <= 1:
        raise InputError("Это «Общая» тема группы — нужна отдельная тема.")
    chat: int | str = int(f"-100{match['cid']}") if match["cid"] else f"@{match['user']}"
    return chat, thread


def overview(window_id: int) -> dict:
    """Сводка окна для экрана ответственного."""
    clubs = league_clubs()
    keys = {norm_club(c) for c in clubs}
    budgets = {norm_club(b["club_name"]) for b in repo.get_club_budgets(window_id)}
    snapshot = repo.core_snapshot_clubs(window_id)
    counts: dict[str, int] = {}
    for t in repo.list_transfers(window_id):
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    return {
        "clubs": len(clubs),
        "budgets": len(keys & budgets),
        "snapshot": len(keys & snapshot),
        "statuses": counts,
        "topics": sorted(repo.get_topics()),
    }


__all__ = [
    "AUTO_REJECT_REASON", "SYSTEM_ACTOR", "InputError", "is_transfer_manager", "can_manage_window",
    "parse_window_datetime", "create_window", "league_clubs", "snapshot_core", "open_window",
    "close_window", "due_auto_close", "set_auto_close", "update_setting", "default_budgets",
    "apply_default_budgets", "resolve_club", "set_budget", "budget_table", "budget_pages", "parse_topic_link",
    "overview", "format_k",
]
