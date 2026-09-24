"""
services/ai/bet_picks.py

Вкладка «ИИ-прогноз» в панели Logovo.bet: открытые исходы линии, отсортированные
по оценке вероятности захода — от самого уверенного к самому неуверенному.

Как считается:
  1. Кандидаты — активные исходы открытых рынков у несыгранных матчей
     (database.get_admin_market_board). Берётся не больше MAX_MATCHES матчей
     и MAX_OPTIONS исходов, по кругу из дивизионов, ближайшие туры первыми, —
     бесплатной модели нельзя отдать всю линию лиги разом. Фильтры по группе
     рынка и диапазону кэфа (normalize_filters) сужают кандидатов ещё до модели.
  2. К каждому матчу — таблица, форма и прогноз ансамбля; к каждому исходу —
     вероятность по линии (1/кэф без маржи букмекера).
  3. Бесплатная модель OpenRouter (OPENROUTER_API_KEY / OPENROUTER_MODEL)
     выбирает самые вероятные исходы и оценивает шанс каждого. Всё, что она
     вернула, сверяется с кандидатами: чужой id или мусор просто отбрасываются.
  4. Нет ключа, модель не ответила или ответила пустым — список строится по
     вероятности линии, и ответ честно помечен source="line".
  5. Исходы, показанные ИИ, пишутся в журнал (database.log_ai_picks), а
     services/ai/pick_review сверяет их с сыгранными матчами.

Результат кэшируется (CACHE_TTL_SECONDS), а ручное обновление не чаще
REFRESH_MIN_SECONDS: у бесплатных моделей дневная квота запросов.
Ключ в логи не попадает.
"""
from __future__ import annotations

import ast
import json
import logging
import re
import threading
import time
import urllib.error
import urllib.request

import config
import database
from time_utils import now_msk_str

logger = logging.getLogger(__name__)

MAX_MATCHES = 24          # матчей в одном запросе к модели
MAX_OPTIONS = 240         # исходов в одном запросе: без фильтров это ~12 матчей,
                          # с фильтром по рынку или кэфу в тот же объём влезает больше
MIN_ODDS = 1.15           # исходы ниже — «заход» без смысла, в прогноз не идут
MAX_FILTER_ODDS = 100.0
PICKS_LIMIT = 30          # сколько исходов отдавать; мин. шанс и value фильтрует клиент
MAX_PER_MATCH = 2         # не больше исходов одного матча — иначе список из одного матча
CACHE_TTL_SECONDS = 30 * 60
FALLBACK_TTL_SECONDS = 5 * 60   # фолбэк держим недолго — модель скоро спросим снова
REFRESH_MIN_SECONDS = 2 * 60
REQUEST_TIMEOUT_SECONDS = 60   # потолок одной модели
# Вся цепочка моделей обязана уложиться в это время: туннель Cloudflare рвёт
# запрос через 100 с, и панель так и висела на «ИИ анализирует линию…».
CHAIN_BUDGET_SECONDS = 75
MIN_ATTEMPT_SECONDS = 10       # меньше осталось — следующую модель не трогаем
# Модель, ответившая 429 или не успевшая, какое-то время не спрашиваем:
# иначе каждый запрос заново ждёт заведомо занятые модели.
RATE_LIMIT_COOLDOWN_SECONDS = 10 * 60
MAX_COOLDOWN_SECONDS = 60 * 60
TIMEOUT_COOLDOWN_SECONDS = 10 * 60
GONE_COOLDOWN_SECONDS = 60 * 60   # 404: модель убрали с OpenRouter
REASON_MAX_CHARS = 220

_FINISHED_MATCH_STATUSES = {"confirmed", "completed", "cancelled"}

_cache: dict[tuple, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_call_lock = threading.Lock()
_cooldowns: dict[str, float] = {}   # модель → time.monotonic(), до которого её пропускаем


# Группы рынков для фильтра вкладки: id → (подпись, market_key движка).
MARKET_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "result": ("Исход", ("1x2",)),
    "double": ("Двойной шанс", ("double_chance",)),
    "total": ("Тотал", ("total_goals",)),
    "itotal": ("Инд. тотал", ("individual_total_1", "individual_total_2")),
    "handicap": ("Фора", ("handicap",)),
    "btts": ("Обе забьют", ("btts",)),
}
_GROUP_BY_MARKET_KEY = {key: gid for gid, (_label, keys) in MARKET_GROUPS.items() for key in keys}


def market_groups() -> list[dict]:
    return [{"id": gid, "label": label} for gid, (label, _keys) in MARKET_GROUPS.items()]


def normalize_filters(markets=None, odds_min=None, odds_max=None) -> dict:
    """Фильтры, влияющие на то, что видит модель. ValueError — на мусор во вводе.

    Мин. шанс и «только ценные» сюда не входят: они не меняют разбор модели
    и фильтруются в клиенте по уже полученному списку, без нового запроса.
    """
    if isinstance(markets, str):
        markets = markets.split(",")
    groups = sorted({str(g).strip() for g in (markets or []) if str(g).strip()})
    unknown = [g for g in groups if g not in MARKET_GROUPS]
    if unknown:
        raise ValueError(f"unknown market group: {', '.join(unknown)}")
    if len(groups) == len(MARKET_GROUPS):
        groups = []  # все группы — то же, что без фильтра, и тот же кэш

    def _odds(value):
        if value is None or str(value).strip() == "":
            return None
        num = float(str(value).replace(",", "."))
        if not (1.0 <= num <= MAX_FILTER_ODDS):
            raise ValueError("odds out of range")
        return round(num, 2)

    lo, hi = _odds(odds_min), _odds(odds_max)
    if lo is not None and hi is not None and lo > hi:
        raise ValueError("odds_min is greater than odds_max")
    return {"markets": groups, "odds_min": lo, "odds_max": hi}


def _cache_key(division_ids: list[int] | None, filters: dict | None = None) -> tuple:
    f = filters or {}
    return (
        tuple(sorted(division_ids)) if division_ids else ("all",),
        tuple(f.get("markets") or ()),
        f.get("odds_min"),
        f.get("odds_max"),
    )


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _cooldowns.clear()


# ─── Кандидаты ──────────────────────────────────────────────────────────────

def _match_overround(markets: list[dict]) -> float:
    """Маржа линии по исходу матча: сумма 1/кэф по П1-Х-П2.

    Движок накладывает одну маржу на все рынки матча, поэтому её можно снять
    с любого исхода делением. Рынка 1X2 нет — маржу не снимаем.
    """
    for mk in markets:
        if mk.get("market_key") != "1x2":
            continue
        odds = [s.get("odds_value") for s in mk.get("selections", [])]
        if len(odds) == 3 and all(o and o > 1 for o in odds):
            total = sum(1.0 / o for o in odds)
            if total >= 1.0:
                return total
    return 1.0


def _round_robin(matches: list[dict], limit: int, max_options: int | None = None) -> list[dict]:
    """Ближайшие туры каждого дивизиона, по одному матчу из дивизиона за круг.

    Останавливается и по числу матчей, и по суммарному числу исходов:
    первый матч берётся всегда, следующий — только если влезает в бюджет.
    """
    by_div: dict[int, list[dict]] = {}
    for m in sorted(matches, key=lambda x: (x.get("round_number") or 0, x["match_id"])):
        by_div.setdefault(m["division_id"], []).append(m)
    queues = [by_div[k] for k in sorted(by_div)]
    picked: list[dict] = []
    options = 0
    while queues and len(picked) < limit:
        for q in list(queues):
            if len(picked) >= limit:
                break
            size = len(q[0]["options"])
            if max_options is not None and picked and options + size > max_options:
                return picked
            picked.append(q.pop(0))
            options += size
            if not q:
                queues.remove(q)
    return picked


def collect_candidates(
    division_ids: list[int] | None,
    max_matches: int = MAX_MATCHES,
    filters: dict | None = None,
    max_options: int | None = MAX_OPTIONS,
) -> list[dict]:
    """Матчи с открытыми рынками и исходами, пригодными для прогноза."""
    f = filters or {}
    allowed_groups = set(f.get("markets") or ())
    odds_lo = max(MIN_ODDS, f.get("odds_min") or 0)
    odds_hi = f.get("odds_max")
    board, _total = database.get_admin_market_board(division_ids, "active", "", 1000, 0)
    matches = []
    for m in board:
        if m.get("match_status") in _FINISHED_MATCH_STATUSES:
            continue
        markets = [mk for mk in m.get("markets", []) if mk.get("status") == "open"]
        overround = _match_overround(markets)
        options = []
        for mk in markets:
            group = _GROUP_BY_MARKET_KEY.get(mk.get("market_key"), "other")
            if allowed_groups and group not in allowed_groups:
                continue
            for s in mk.get("selections", []):
                odds = s.get("odds_value")
                if s.get("status") != "active" or not odds or odds < odds_lo:
                    continue
                if odds_hi is not None and odds > odds_hi:
                    continue
                options.append({
                    "selection_id": s["id"],
                    "market_id": mk.get("id"),
                    "market_key": mk.get("market_key"),
                    "market_group": group,
                    "market_name": mk.get("market_name"),
                    "selection_key": s.get("selection_key"),
                    "selection_name": s.get("selection_name"),
                    "odds": round(float(odds), 2),
                    "line_probability": round(min(99.0, 100.0 / (float(odds) * overround)), 1),
                })
        if options:
            matches.append({
                "match_id": m["match_id"],
                "division_id": m.get("division_id"),
                "division_name": m.get("division_name"),
                "cup_label": m.get("cup_label"),
                "round_number": m.get("round_number"),
                "cup_series_id": m.get("cup_series_id"),
                "tournament_type": m.get("tournament_type"),
                "cup_stage": m.get("cup_stage"),
                "game_num_in_series": m.get("game_num_in_series"),
                "team1": m.get("team1_name"),
                "team2": m.get("team2_name"),
                "options": options,
            })
    return _round_robin(matches, max_matches, max_options)


def _team_context(standings: list[dict], form_map: dict, team: str) -> dict:
    position, row = None, None
    for idx, r in enumerate(standings, start=1):
        if r.get("team_name") == team:
            position, row = idx, r
            break
    form = form_map.get((team or "").lower()) or []
    ctx: dict = {"form": "".join(form)}
    if row:
        ctx.update({
            "pos": position,
            "pts": row.get("points"),
            "played": row.get("played"),
            "gf": row.get("goals_scored"),
            "ga": row.get("goals_conceded"),
        })
    return ctx


def _enrich(matches: list[dict]) -> None:
    """Таблица, форма и прогноз ансамбля. Любой сбой — матч просто без контекста."""
    tables: dict[int, tuple[list, dict]] = {}
    for m in matches:
        div = m.get("division_id") or 1
        try:
            if div not in tables:
                tables[div] = (database.get_standings(division_id=div),
                               database.get_teams_recent_form(5, division_id=div))
            standings, form_map = tables[div]
            m["team1_ctx"] = _team_context(standings, form_map, m["team1"])
            m["team2_ctx"] = _team_context(standings, form_map, m["team2"])
        except Exception:
            logger.warning("AI picks: no table context for match %s", m["match_id"], exc_info=True)
        try:
            from services.ensemble_engine import EnsemblePredictionEngine
            p = EnsemblePredictionEngine.predict_match(m["match_id"], save_to_db=False)
            goals = p.get("goals_markets") or {}
            m["model"] = {
                "p1": round(p["home_probability"] * 100, 1),
                "x": round(p["draw_probability"] * 100, 1),
                "p2": round(p["away_probability"] * 100, 1),
                "xg": [p["expected_goals"]["team1"], p["expected_goals"]["team2"]],
                "over_2_5": round((goals.get("over_2_5") or 0) * 100, 1),
                "btts": round((goals.get("btts_yes") or 0) * 100, 1),
            }
        except Exception:
            logger.debug("AI picks: no ensemble prediction for match %s", m["match_id"], exc_info=True)


# ─── Модель ─────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "Ты — аналитик виртуальной букмекерской линии киберфутбольной лиги (EA FC, игроки-люди, "
    "реальные деньги не участвуют). Тебе дают матчи с таблицей, формой (W/D/L, свежий матч слева), "
    "прогнозом статистической модели и списком исходов: [id, исход, коэффициент, вероятность по линии %].\n"
    "Задача: выбери до {limit} исходов с НАИБОЛЬШЕЙ вероятностью захода и оцени вероятность каждого "
    "в процентах. Не больше {per_match} исходов на матч. Опирайся на данные, а не на названия клубов: "
    "реальная сила клуба тут не важна, играют люди.\n"
    "Ответ — ТОЛЬКО JSON без пояснений и без markdown:\n"
    '{{"picks": [{{"id": <id исхода>, "probability": <число 1-99>, "reason": "<коротко по-русски, до 120 символов>"}}]}}'
)


def _payload_for_model(matches: list[dict]) -> list[dict]:
    out = []
    for m in matches:
        item = {
            "match_id": m["match_id"],
            "match": f'{m["team1"]} — {m["team2"]}',
            "division": m.get("division_name"),
            "round": m.get("round_number"),
            "home": m.get("team1_ctx"),
            "away": m.get("team2_ctx"),
            "options": [[o["selection_id"], o["selection_name"], o["odds"], o["line_probability"]]
                        for o in m["options"]],
        }
        if m.get("model"):
            item["model"] = m["model"]
        out.append(item)
    return out


def _models() -> list[str]:
    return [x.strip() for x in (config.OPENROUTER_MODEL or "").split(",") if x.strip()]


def _extract_json(text: str) -> dict | None:
    """JSON из ответа модели: устойчив к markdown, рассуждениям, trailing commas и обрезке."""
    if not text:
        return None
    # Рассуждающие модели (Qwen3, Space Bunny) могут писать размышления в тегах.
    # Очищаем как закрытые, так и незакрытые теги рассуждений.
    cleaned = re.sub(r"<(?:think|thought|reasoning)>.*?</(?:think|thought|reasoning)>", "", text, flags=re.S | re.I)
    cleaned = re.sub(r"<(?:think|thought|reasoning)>.*?(?=(?:```|\{))", "", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<(?:think|thought|reasoning)>.*$", "", cleaned, flags=re.S | re.I)

    candidates = []
    # 1. Сначала проверяем блок кода markdown: ```json ... ```
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, flags=re.S)
    if fence:
        candidates.append(fence.group(1).strip())

    # 2. Внешние фигурные скобки
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        candidates.append(cleaned[start:end + 1].strip())

    for c in candidates:
        for variant in (c, re.sub(r",\s*([\]}])", r"\1", c)):
            try:
                data = json.loads(variant)
                if isinstance(data, dict):
                    return data
            except ValueError:
                pass
            try:
                data = ast.literal_eval(variant)
                if isinstance(data, dict):
                    return data
            except Exception:
                pass

    # 3. Фолбэк: если JSON оборвался на полпути или окружён текстом со скобками,
    # спасаем уже сгенерированные полные объекты исходов {"id": ...}
    items = []
    for m in re.finditer(r"\{[^{}]*\"id\"[^{}]*\}", cleaned):
        clean_item = re.sub(r",\s*([\]}])", r"\1", m.group(0))
        try:
            obj = json.loads(clean_item)
            if isinstance(obj, dict) and "id" in obj:
                items.append(obj)
                continue
        except Exception:
            pass
        try:
            obj = ast.literal_eval(clean_item)
            if isinstance(obj, dict) and "id" in obj:
                items.append(obj)
        except Exception:
            pass
    if items:
        return {"picks": items}

    return None


def _call_openrouter(matches: list[dict]) -> tuple[dict | None, str | None]:
    """(разобранный JSON, модель) или (None, None). Модели пробуются по очереди."""
    api_key = config.OPENROUTER_API_KEY
    if not api_key:
        return None, None
    base_url = (config.OPENROUTER_BASE_URL or "https://openrouter.ai/api/v1").rstrip("/")
    system = _SYSTEM_PROMPT.format(limit=PICKS_LIMIT, per_match=MAX_PER_MATCH)
    user = "ДАННЫЕ (JSON):\n" + json.dumps(_payload_for_model(matches), ensure_ascii=False)

    started = time.monotonic()
    for model in _models():
        now = time.monotonic()
        with _cache_lock:
            until = _cooldowns.get(model, 0.0)
        if until > now:
            logger.info("AI picks: model '%s' is cooling down for %d s more, skipping it.", model, until - now)
            continue
        timeout = min(REQUEST_TIMEOUT_SECONDS, CHAIN_BUDGET_SECONDS - (now - started))
        if timeout < MIN_ATTEMPT_SECONDS:
            logger.warning("AI picks: time budget spent, model '%s' and the rest are not tried.", model)
            break
        body = json.dumps({
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0.2,
            # У рассуждающих моделей размышления входят в max_tokens: без запаса
            # и короткого reasoning ответ обрывается, не дойдя до JSON.
            "max_tokens": 16000,
            "reasoning": {"effort": "low", "exclude": True},
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": "Logovo.bet",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err_msg = ""
            try:
                err_msg = e.read().decode("utf-8", errors="replace")[:250].strip()
            except Exception:
                pass
            pause = _http_cooldown(e)
            if pause:
                _cool_down(model, pause)
            logger.warning("AI picks: model '%s' HTTP %s (%s)%s, trying the next one.", model, e.code, err_msg,
                           f", skipped for {pause} s" if pause else "")
            continue
        except (TimeoutError, urllib.error.URLError) as e:
            if not isinstance(e, TimeoutError) and not isinstance(getattr(e, "reason", None), TimeoutError):
                logger.warning("AI picks: model '%s' failed (%s), trying the next one.", model, type(e).__name__)
                continue
            _cool_down(model, TIMEOUT_COOLDOWN_SECONDS)
            logger.warning("AI picks: model '%s' timed out after %d s (skipped for %d s), trying the next one.",
                           model, timeout, TIMEOUT_COOLDOWN_SECONDS)
            continue
        except Exception as e:
            logger.warning("AI picks: model '%s' failed (%s), trying the next one.", model, type(e).__name__)
            continue
        try:
            choice = result["choices"][0]
            msg = choice["message"]
            # Часть моделей при exclude-рассуждениях оставляет content пустым,
            # а ответ целиком кладёт в reasoning.
            text = msg.get("content") or msg.get("reasoning") or msg.get("reasoning_content") or ""
        except (KeyError, IndexError, TypeError):
            logger.warning("AI picks: model '%s' returned no choices.", model)
            continue
        data = _extract_json(text if isinstance(text, str) else "")
        if data is not None:
            return data, model
        snippet = (text[:250] if isinstance(text, str) else "")
        logger.warning(
            "AI picks: model '%s' returned no parsable JSON (finish_reason=%s, len=%d, snippet=%r).",
            model, choice.get("finish_reason"), len(text) if isinstance(text, str) else 0, snippet,
        )
    return None, None


def _http_cooldown(e: urllib.error.HTTPError) -> int:
    """Сколько секунд не спрашивать модель после ошибки; 0 — спрашивать как обычно."""
    if e.code == 404:
        return GONE_COOLDOWN_SECONDS
    if e.code != 429:
        return 0
    headers = e.headers or {}
    wait = None
    try:
        # Retry-After — секунды; X-RateLimit-Reset у OpenRouter — эпоха в миллисекундах.
        if headers.get("Retry-After"):
            wait = float(headers["Retry-After"])
        elif headers.get("X-RateLimit-Reset"):
            wait = float(headers["X-RateLimit-Reset"]) / 1000.0 - time.time()
    except (TypeError, ValueError):
        wait = None
    if wait is None or wait <= 0:
        return RATE_LIMIT_COOLDOWN_SECONDS
    return int(min(max(wait, 60), MAX_COOLDOWN_SECONDS))


def _cool_down(model: str, seconds: int) -> None:
    with _cache_lock:
        _cooldowns[model] = time.monotonic() + seconds


# ─── Ранжирование ───────────────────────────────────────────────────────────

def _pick_row(match: dict, option: dict, probability: float, reason: str) -> dict:
    return {
        "selection_id": option["selection_id"],
        "match_id": match["match_id"],
        "division_id": match.get("division_id"),
        "division_name": match.get("division_name"),
        "cup_label": match.get("cup_label"),
        "round_number": match.get("round_number"),
        # Для сборщика купона: исход ставится по market_id + selection_id, а игры
        # одной кубковой серии нельзя класть в один экспресс.
        "cup_series_id": match.get("cup_series_id"),
        # Подпись матча в Mini App (matchRoundLabel): стадия и номер игры кубка.
        "tournament_type": match.get("tournament_type"),
        "cup_stage": match.get("cup_stage"),
        "game_num_in_series": match.get("game_num_in_series"),
        "team1": match["team1"],
        "team2": match["team2"],
        "market_id": option.get("market_id"),
        "market_key": option.get("market_key"),
        "market_name": option["market_name"],
        "market_group": option.get("market_group"),
        "selection_key": option.get("selection_key"),
        "selection_name": option["selection_name"],
        "odds": option["odds"],
        "probability": round(probability, 1),
        "line_probability": option["line_probability"],
        # Ценность: >0 — модель видит шанс выше, чем заложено в кэф.
        "value": round(probability / 100.0 * option["odds"] - 1.0, 3),
        "reason": reason,
    }


def _limited(rows: list[dict]) -> list[dict]:
    rows.sort(key=lambda r: (-r["probability"], r["odds"], r["selection_id"]))
    per_match: dict[int, int] = {}
    out = []
    for r in rows:
        if per_match.get(r["match_id"], 0) >= MAX_PER_MATCH:
            continue
        per_match[r["match_id"]] = per_match.get(r["match_id"], 0) + 1
        out.append(r)
        if len(out) >= PICKS_LIMIT:
            break
    return out


def rank_ai_picks(matches: list[dict], data: dict) -> list[dict]:
    """Ответ модели → строки. Id, которых нет среди кандидатов, отбрасываются."""
    index = {o["selection_id"]: (m, o) for m in matches for o in m["options"]}
    raw = data.get("picks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    rows, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            sel_id = int(item.get("id"))
            probability = float(item.get("probability"))
        except (TypeError, ValueError):
            continue
        if sel_id in seen or sel_id not in index or probability != probability:
            continue
        seen.add(sel_id)
        match, option = index[sel_id]
        probability = max(1.0, min(99.0, probability))
        reason = str(item.get("reason") or "").strip()[:REASON_MAX_CHARS]
        rows.append(_pick_row(match, option, probability, reason))
    return _limited(rows)


def rank_line_picks(matches: list[dict]) -> list[dict]:
    """Фолбэк без модели: вероятность по линии, маржа снята."""
    rows = [_pick_row(m, o, o["line_probability"], "") for m in matches for o in m["options"]]
    return _limited(rows)


# ─── Точка входа ────────────────────────────────────────────────────────────

def _log_for_review(picks: list[dict], model: str | None) -> None:
    """Показанные ИИ исходы — в журнал для сверки (services/ai/pick_review).
    Журнал вторичен: его сбой не должен отнимать у админа сам прогноз."""
    try:
        database.log_ai_picks(picks, model)
    except Exception:
        logger.exception("AI picks: failed to log picks for the review")


def build_picks(division_ids: list[int] | None, filters: dict | None = None) -> dict:
    filters = filters or normalize_filters()
    matches = collect_candidates(division_ids, filters=filters)
    result = {
        "source": "line",
        "model": None,
        "ai_configured": bool(config.OPENROUTER_API_KEY and _models()),
        "error": None,
        "generated_at": now_msk_str(),
        "matches_considered": len(matches),
        "options_considered": sum(len(m["options"]) for m in matches),
        "filters": filters,
        "market_groups": market_groups(),
        "picks": [],
    }
    if not matches:
        return result
    _enrich(matches)

    if result["ai_configured"]:
        data, model = _call_openrouter(matches)
        picks = rank_ai_picks(matches, data) if data is not None else []
        if picks:
            result.update(source="ai", model=model, picks=picks)
            _log_for_review(picks, model)
            return result
        result["error"] = "ai_unavailable"
    else:
        result["error"] = "no_key"
    result["picks"] = rank_line_picks(matches)
    return result


def get_picks(division_ids: list[int] | None, refresh: bool = False, filters: dict | None = None) -> dict:
    """Прогноз из кэша или свежий. Одновременно в модель идёт только один запрос.

    Кэш свой у каждого сочетания дивизиона и фильтров (normalize_filters).
    """
    filters = filters or normalize_filters()
    key = _cache_key(division_ids, filters)
    now = time.monotonic()
    with _cache_lock:
        cached = _cache.get(key)
    if cached:
        ts, data = cached
        age = now - ts
        ttl = CACHE_TTL_SECONDS if data["source"] == "ai" else FALLBACK_TTL_SECONDS
        if age < ttl and not (refresh and age >= REFRESH_MIN_SECONDS):
            return {**data, "cached": True, "refresh_in": int(max(0, REFRESH_MIN_SECONDS - age))}

    with _call_lock:
        # Пока ждали замок, соседний запрос мог уже всё посчитать.
        with _cache_lock:
            cached = _cache.get(key)
        if cached and cached[0] > now:
            return {**cached[1], "cached": True, "refresh_in": REFRESH_MIN_SECONDS}
        data = build_picks(division_ids, filters)
        with _cache_lock:
            _cache[key] = (time.monotonic(), data)
    return {**data, "cached": False, "refresh_in": REFRESH_MIN_SECONDS}
