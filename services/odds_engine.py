"""
services/odds_engine.py

Logovo.bet — Core Odds Engine & Market Management Service (v2.0).
Provides:
1. Dynamic odds calculation & margin adjustment.
2. Relational market & selection lifecycle (create, update, lock, suspend).
3. Immutable odds movement history & audit trails.
4. Server-authoritative odds validation with slippage/drift protection.
"""

import math
import logging
from typing import Optional
import database
from services.poisson_odds import TARGET_MARGIN

logger = logging.getLogger(__name__)

# Shared with services.betting_engine: the line tiles and market_selections must agree.
BOOKMAKER_MARGIN = TARGET_MARGIN

# Автоматический пересчёт двигает любой коэффициент матча не больше чем на ±15%
# (в 1.15 раза в любую сторону) за одно изменение модели. В начале сезона
# таблица из 1–2 игр раскачивала линию в разы за вечер.
MAX_REPRICE_STEP = 0.15


def get_or_create_market(
    match_id: int,
    market_key: str,
    market_name: str,
    category: str = "main",
    sort_order: int = 0
) -> dict:
    """Ensure a market exists for a match and return its record."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM markets WHERE match_id = ? AND market_key = ?",
            (match_id, market_key)
        )
        row = cursor.fetchone()
        if row:
            return dict(row)

        cursor.execute("""
            INSERT INTO markets (match_id, market_key, market_name, category, status, sort_order, created_at)
            VALUES (?, ?, ?, ?, 'open', ?, datetime('now', '+3 hours'))
        """, (match_id, market_key, market_name, category, sort_order))
        m_id = cursor.lastrowid
        cursor.execute("SELECT * FROM markets WHERE id = ?", (m_id,))
        return dict(cursor.fetchone())


def get_or_create_selection(
    market_id: int,
    selection_key: str,
    selection_name: str,
    initial_odds: float,
    update_odds: bool = False,
    model_odds: Optional[float] = None
) -> dict:
    """Ensure a selection exists within a market and return its record.

    ``model_odds`` stores the raw model price the (possibly smoothed)
    ``initial_odds`` was derived from; see ``smooth_match_repricing``.
    """
    initial_odds = round(float(initial_odds), 2)
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM market_selections WHERE market_id = ? AND selection_key = ?",
            (market_id, selection_key)
        )
        row = cursor.fetchone()
        if row:
            if model_odds is not None and (
                row["model_odds"] is None or abs(float(row["model_odds"]) - model_odds) > 0.001
            ):
                cursor.execute(
                    "UPDATE market_selections SET model_odds = ? WHERE id = ?",
                    (model_odds, row["id"]),
                )
                cursor.execute("SELECT * FROM market_selections WHERE id = ?", (row["id"],))
                row = cursor.fetchone()
            if update_odds and (abs(float(row["odds_value"]) - initial_odds) > 0.001 or row["selection_name"] != selection_name):
                old_val = float(row["odds_value"])
                new_version = row["odds_version"] + 1
                cursor.execute("""
                    UPDATE market_selections
                    SET previous_odds = odds_value,
                        odds_value = ?,
                        odds_version = ?,
                        selection_name = ?,
                        updated_at = datetime('now', '+3 hours')
                    WHERE id = ?
                """, (initial_odds, new_version, selection_name, row["id"]))

                # Audit movement and history if odds value shifted
                if abs(old_val - initial_odds) > 0.001:
                    cursor.execute("""
                        INSERT INTO odds_history (selection_id, old_value, new_value, changed_by, reason, changed_at)
                        VALUES (?, ?, ?, NULL, 'repricing_sync', datetime('now', '+3 hours'))
                    """, (row["id"], old_val, initial_odds))
                    try:
                        cursor.execute("SELECT match_id FROM markets WHERE id = ?", (market_id,))
                        mkt_row = cursor.fetchone()
                        if mkt_row:
                            match_id = mkt_row["match_id"]
                            pct_change = round(((initial_odds - old_val) / max(0.01, old_val)) * 100, 2)
                            direction = "up" if initial_odds > old_val else "down"
                            cursor.execute("""
                                INSERT INTO odds_movement (selection_id, market_id, match_id, old_odds, new_odds, pct_change, direction, velocity, reason, source, created_at)
                                VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, 'repricing_sync', 'system', datetime('now', '+3 hours'))
                            """, (row["id"], market_id, match_id, old_val, initial_odds, pct_change, direction))
                    except Exception as e:
                        logger.warning(f"Failed to record odds_movement: {e}")

                cursor.execute("SELECT * FROM market_selections WHERE id = ?", (row["id"],))
                return dict(cursor.fetchone())
            return dict(row)

        cursor.execute("""
            INSERT INTO market_selections (market_id, selection_key, selection_name, odds_value, odds_version, status, previous_odds, model_odds, updated_at)
            VALUES (?, ?, ?, ?, 1, 'active', NULL, ?, datetime('now', '+3 hours'))
        """, (market_id, selection_key, selection_name, initial_odds, model_odds))
        sel_id = cursor.lastrowid
        cursor.execute("SELECT * FROM market_selections WHERE id = ?", (sel_id,))
        return dict(cursor.fetchone())


def set_odds(
    market_id: int,
    selection_key: str,
    value: float,
    admin_id: Optional[int] = None,
    reason: Optional[str] = None
) -> dict:
    """
    Update odds value for a selection, increment version, track previous odds,
    and append an immutable record to odds_history.
    """
    new_value = round(float(value), 2)
    if new_value < 1.01:
        raise ValueError("Коэффициент не может быть меньше 1.01")

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM market_selections WHERE market_id = ? AND selection_key = ?",
            (market_id, selection_key)
        )
        sel = cursor.fetchone()
        if not sel:
            raise ValueError(f"Selection '{selection_key}' in market #{market_id} not found.")

        sel_id = sel["id"]
        old_value = sel["odds_value"]
        new_version = sel["odds_version"] + 1

        if abs(old_value - new_value) > 0.001:
            cursor.execute("""
                UPDATE market_selections
                SET previous_odds = odds_value,
                    odds_value = ?,
                    odds_version = ?,
                    updated_at = datetime('now', '+3 hours')
                WHERE id = ?
            """, (new_value, new_version, sel_id))

            cursor.execute("""
                INSERT INTO odds_history (selection_id, old_value, new_value, changed_by, reason, changed_at)
                VALUES (?, ?, ?, ?, ?, datetime('now', '+3 hours'))
            """, (sel_id, old_value, new_value, admin_id, reason or "odds_update"))

            # Phase 6: Odds Movement Tracking & Canonical Sync
            try:
                cursor.execute("SELECT match_id FROM markets WHERE id = ?", (market_id,))
                mkt_row = cursor.fetchone()
                if mkt_row:
                    match_id = mkt_row["match_id"]
                    pct_change = round(((new_value - old_value) / max(0.01, old_value)) * 100, 2)
                    direction = "up" if new_value > old_value else "down"
                    cursor.execute("""
                        INSERT INTO odds_movement (selection_id, market_id, match_id, old_odds, new_odds, pct_change, direction, velocity, reason, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?, datetime('now', '+3 hours'))
                    """, (sel_id, market_id, match_id, old_value, new_value, pct_change, direction, reason or "odds_update", f"admin:{admin_id}" if admin_id else "system"))

                    # Synchronize bet_markets for canonical consistency
                    bm_col_map = {
                        "p1": "odd_p1", "x": "odd_x", "p2": "odd_p2",
                        "over_2.5": "odd_tb25", "tb25": "odd_tb25",
                        "under_2.5": "odd_tm25", "tm25": "odd_tm25",
                        "btts_yes": "odd_btts_yes", "btts_no": "odd_btts_no"
                    }
                    col_name = bm_col_map.get(selection_key)
                    if col_name:
                        cursor.execute(f"UPDATE bet_markets SET {col_name} = ? WHERE match_id = ?", (new_value, match_id))
            except Exception as e:
                logger.warning(f"Failed to record odds_movement/sync: {e}")

        cursor.execute("SELECT * FROM market_selections WHERE id = ?", (sel_id,))
        return dict(cursor.fetchone())


def get_current_odds(market_id: int, selection_key: str) -> float:
    """Retrieve current decimal odds for a specific selection."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT odds_value, status FROM market_selections WHERE market_id = ? AND selection_key = ?",
            (market_id, selection_key)
        )
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Selection '{selection_key}' in market #{market_id} not found.")
        if row["status"] != "active":
            raise ValueError(f"Selection is currently {row['status']}.")
        return float(row["odds_value"])


def get_odds_history(
    selection_id: Optional[int] = None,
    market_id: Optional[int] = None,
    selection_key: Optional[str] = None,
    limit: int = 20
) -> list[dict]:
    """Retrieve chronological history of odds movements."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        if selection_id:
            cursor.execute("""
                SELECT h.*, u.username as admin_username
                FROM odds_history h
                LEFT JOIN users u ON h.changed_by = u.telegram_id
                WHERE h.selection_id = ?
                ORDER BY h.id DESC
                LIMIT ?
            """, (selection_id, limit))
        elif market_id and selection_key:
            cursor.execute("""
                SELECT h.*, u.username as admin_username
                FROM odds_history h
                JOIN market_selections s ON h.selection_id = s.id
                LEFT JOIN users u ON h.changed_by = u.telegram_id
                WHERE s.market_id = ? AND s.selection_key = ?
                ORDER BY h.id DESC
                LIMIT ?
            """, (market_id, selection_key, limit))
        else:
            return []

        return [dict(r) for r in cursor.fetchall()]


def suspend_market(market_id: int, admin_id: Optional[int] = None, reason: Optional[str] = None) -> bool:
    """Suspend a market (e.g. during live action or VAR check)."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE markets SET status = 'suspended' WHERE id = ?",
            (market_id,)
        )
        if admin_id:
            cursor.execute("""
                INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, new_value, reason, created_at)
                VALUES (?, 'suspend_market', 'market', ?, 'suspended', ?, datetime('now', '+3 hours'))
            """, (admin_id, market_id, reason))
        return cursor.rowcount > 0


def unsuspend_market(market_id: int, admin_id: Optional[int] = None) -> bool:
    """Reopen a suspended market."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE markets SET status = 'open' WHERE id = ?",
            (market_id,)
        )
        if admin_id:
            cursor.execute("""
                INSERT INTO admin_audit_log (admin_id, action, target_type, target_id, new_value, reason, created_at)
                VALUES (?, 'unsuspend_market', 'market', ?, 'open', 'Manual unsuspend', datetime('now', '+3 hours'))
            """, (admin_id, market_id))
        return cursor.rowcount > 0


def lock_selection(selection_id: int, admin_id: Optional[int] = None) -> bool:
    """Lock a specific selection outcome."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE market_selections SET status = 'locked' WHERE id = ?",
            (selection_id,)
        )
        return cursor.rowcount > 0


def unlock_selection(selection_id: int, admin_id: Optional[int] = None) -> bool:
    """Unlock a locked selection outcome."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE market_selections SET status = 'active' WHERE id = ?",
            (selection_id,)
        )
        return cursor.rowcount > 0


def validate_odds(
    market_id: int,
    selection_key: str,
    expected_odd: Optional[float] = None,
    max_drift: float = 0.05
) -> float:
    """
    Validate that market is open, selection is active, and odds have not drifted
    beyond the acceptable threshold (default 5%).
    Returns server authoritative odds.
    """
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT m.status as market_status, s.status as sel_status, s.odds_value
            FROM market_selections s
            JOIN markets m ON s.market_id = m.id
            WHERE s.market_id = ? AND s.selection_key = ?
        """, (market_id, selection_key))
        row = cursor.fetchone()

        if not row:
            raise ValueError(f"Selection '{selection_key}' in market #{market_id} does not exist.")

        if row["market_status"] != "open":
            raise ValueError(f"Рынок недоступен (статус: {row['market_status']}).")

        if row["sel_status"] != "active":
            raise ValueError(f"Исход заблокирован (статус: {row['sel_status']}).")

        current_odd = float(row["odds_value"])

        if expected_odd is not None:
            drift = abs(current_odd - expected_odd)
            if drift > max_drift:
                raise ValueError(
                    f"Коэффициент изменился: было {expected_odd}, стало {current_odd}. Обновите купон."
                )

        return current_odd


def _current_selection_prices(match_id: int) -> dict:
    """{(market_key, selection_key): (odds_value, model_odds)} for a match."""
    with database.transaction() as conn:
        rows = conn.execute("""
            SELECT m.market_key, s.selection_key, s.odds_value, s.model_odds
            FROM market_selections s
            JOIN markets m ON m.id = s.market_id
            WHERE m.match_id = ?
        """, (match_id,)).fetchall()
    return {
        (r["market_key"], r["selection_key"]): (float(r["odds_value"]), r["model_odds"])
        for r in rows
    }


def smooth_match_repricing(current: dict, targets: dict, max_step: float = MAX_REPRICE_STEP) -> dict:
    """Step a match's odds toward the fresh model prices by at most ±max_step.

    ``current`` maps key -> (odds_value, model_odds) for selections that already
    exist; ``targets`` maps key -> fresh model odd. Returns key -> odd to write.

    * A selection with no row yet gets its target straight away.
    * If the model has not changed since the last reprice (every stored
      model_odds equals its target), the line stays put: re-opening the line
      in the Mini App must not keep stepping toward the target.
    * Otherwise every selection of the match moves by the same fraction ``t``
      of the way from its current to its target implied probability, ``t``
      being the largest value that keeps each odd within x(1 +/- max_step).
      A shared ``t`` keeps the line a blend of two margin-bearing books, so no
      set of outcomes covering the match can be priced below 100% (no
      arbitrage), which clamping every odd on its own would not guarantee.
    """
    existing = {k: v for k, v in current.items() if k in targets}
    model_changed = any(
        model is None or abs(float(model) - targets[k]) > 0.001
        for k, (_, model) in existing.items()
    )

    t = 1.0
    if existing and model_changed:
        for k, (cur, _) in existing.items():
            p0, p1 = 1.0 / cur, 1.0 / targets[k]
            if abs(p1 - p0) < 1e-12:
                continue
            bound = p0 * (1 + max_step) if p1 > p0 else p0 / (1 + max_step)
            t = min(t, (bound - p0) / (p1 - p0))
        t = max(0.0, t)

    result = {}
    for k, target in targets.items():
        if k not in existing:
            result[k] = target
        elif not model_changed:
            result[k] = existing[k][0]
        else:
            p0, p1 = 1.0 / existing[k][0], 1.0 / target
            result[k] = max(1.01, round(1.0 / (p0 + t * (p1 - p0)), 2))
    return result


def get_match_markets(match_id: int) -> list[dict]:
    """Retrieve all markets and selections for a match formatted for API & UI."""
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, match_id, market_key, market_name, category, status, sort_order
            FROM markets
            WHERE match_id = ?
            ORDER BY sort_order ASC, id ASC
        """, (match_id,))
        market_rows = [dict(r) for r in cursor.fetchall()]

        for m in market_rows:
            cursor.execute("""
                SELECT id, market_id, selection_key, selection_name, odds_value, 
                       odds_version, status, previous_odds, updated_at
                FROM market_selections
                WHERE market_id = ?
                ORDER BY id ASC
            """, (m["id"],))
            m["selections"] = [dict(s) for s in cursor.fetchall()]

        return market_rows


def apply_market_spec(match_id: int, spec: list) -> list[dict]:
    """Записать спеку рынков матча в `markets` / `market_selections`.

    Механизм один для лиги и кубка: строка спеки —
    `(market_key, market_name, category, sort_order, [(selection_key, name, odd)])`,
    цены шагают к модели не больше чем на ±15% за пересчёт
    (`smooth_match_repricing`), а `model_odds` хранит сырую цену модели, чтобы
    повторный показ линии не продолжал шагать к той же цели.

    Исход с ценой `None` не записывается вовсе: `market_selections.odds_value` —
    NOT NULL, а нулевая вероятность (ф1(-1.5) при_lambda 0.4) означает «такого
    рынка нет», а не «коэффициент ноль».
    """
    targets = {}
    for mk, _m_name, _cat, _sort, sels in spec:
        for sk, _s_name, odd in sels:
            if odd is None:
                continue
            targets[(mk, sk)] = round(float(odd), 2)

    priced = smooth_match_repricing(_current_selection_prices(match_id), targets)

    for mk, m_name, category, sort_order, sels in spec:
        market = get_or_create_market(match_id, mk, m_name, category=category, sort_order=sort_order)
        for sk, s_name, odd in sels:
            if odd is None:
                continue
            get_or_create_selection(
                market["id"], sk, s_name, priced[(mk, sk)],
                update_odds=True, model_odds=targets[(mk, sk)],
            )

    return get_match_markets(match_id)


def generate_match_markets(
    match_id: int,
    team1_name: str,
    team2_name: str,
    standings: Optional[list[dict]] = None
) -> list[dict]:
    """
    Generate Tier 1 standard markets for a match:
    - 1X2 (Match Winner)
    - Double Chance (1X, 12, X2)
    - Total Goals (Over/Under 1.5, 2.5, 3.5)
    - Individual Totals (Team 1 & Team 2 Over/Under 1.5)
    - Both Teams to Score (Yes/No)
    - Handicap (-1.5 / +1.5)
    """
    m = None
    try:
        m = database.get_match(match_id)
    except Exception as e:
        logger.debug(f"Could not load match #{match_id}: {e}")

    if m and database.match_is_cup(m):
        # Кубок лиговой моделью не ценится: в ней есть ничья, а в кубке её нет, и
        # у заголовка серии нет даже имён клубов. Линию этапа выставляет
        # `betting_engine.generate_stage_markets` — здесь только читаем её.
        logger.warning("generate_match_markets refused for cup match #%s", match_id)
        return get_match_markets(match_id)

    p1_nick = (m.get("player1_nickname") or m.get("player1_username")) if m else None
    p2_nick = (m.get("player2_nickname") or m.get("player2_username")) if m else None

    if standings is None:
        try:
            m_div = m.get("division_id") if m else None
            m_season = m.get("season_id") if m else None
            standings = database.get_standings(division_id=m_div, season_id=m_season)
        except Exception as e:
            logger.warning(f"Could not load standings for match #{match_id}: {e}")
            standings = []

    from services.betting_engine import _get_team_strength_score
    from services.poisson_odds import calculate_poisson_market_odds

    s1 = _get_team_strength_score(standings, team1_name, nickname=p1_nick)
    s2 = _get_team_strength_score(standings, team2_name, nickname=p2_nick)

    # Calculate all markets from unified bivariate Poisson distribution with 7.5% margin
    odds = calculate_poisson_market_odds(s1, s2, margin=BOOKMAKER_MARGIN)

    odd_p1 = odds["odd_p1"]
    odd_x = odds["odd_x"]
    odd_p2 = odds["odd_p2"]

    odd_1x = odds["odd_1x"]
    odd_12 = odds["odd_12"]
    odd_x2 = odds["odd_x2"]

    odd_tb15, odd_tm15 = odds["odd_tb15"], odds["odd_tm15"]
    odd_tb25, odd_tm25 = odds["odd_tb25"], odds["odd_tm25"]
    odd_tb35, odd_tm35 = odds["odd_tb35"], odds["odd_tm35"]

    odd_btts_yes = odds["odd_btts_yes"]
    odd_btts_no = odds["odd_btts_no"]

    ind1_over, ind1_under = odds["odd_itb1"], odds["odd_itm1"]
    ind2_over, ind2_under = odds["odd_itb2"], odds["odd_itm2"]

    odd_h1_minus = odds["odd_h1_minus_1.5"]
    odd_h2_plus = odds["odd_h2_plus_1.5"]
    odd_h1_plus = odds["odd_h1_plus_1.5"]
    odd_h2_minus = odds["odd_h2_minus_1.5"]

    # (market_key, market_name, category, sort_order, [(selection_key, name, odd)])
    spec = [
        ("1x2", "Исход матча", "main", 1, [
            ("p1", f"П1 ({team1_name})", odd_p1),
            ("x", "Ничья (X)", odd_x),
            ("p2", f"П2 ({team2_name})", odd_p2),
        ]),
        ("double_chance", "Двойной шанс", "main", 2, [
            ("1x", "1X (П1 или Х)", odd_1x),
            ("12", "12 (П1 или П2)", odd_12),
            ("x2", "X2 (Х или П2)", odd_x2),
        ]),
        ("total_goals", "Тотал голов", "goals", 3, [
            ("over_1.5", "Тотал больше (1.5)", odd_tb15),
            ("under_1.5", "Тотал меньше (1.5)", odd_tm15),
            ("over_2.5", "Тотал больше (2.5)", odd_tb25),
            ("under_2.5", "Тотал меньше (2.5)", odd_tm25),
            ("over_3.5", "Тотал больше (3.5)", odd_tb35),
            ("under_3.5", "Тотал меньше (3.5)", odd_tm35),
        ]),
        ("btts", "Обе забьют", "goals", 4, [
            ("btts_yes", "Обе забьют: Да", odd_btts_yes),
            ("btts_no", "Обе забьют: Нет", odd_btts_no),
        ]),
        ("individual_total_1", f"Инд. тотал: {team1_name}", "goals", 5, [
            ("it1_over_1.5", "ИТБ1 (1.5)", ind1_over),
            ("it1_under_1.5", "ИТМ1 (1.5)", ind1_under),
        ]),
        ("individual_total_2", f"Инд. тотал: {team2_name}", "goals", 6, [
            ("it2_over_1.5", "ИТБ2 (1.5)", ind2_over),
            ("it2_under_1.5", "ИТМ2 (1.5)", ind2_under),
        ]),
        ("handicap", "Фора (1.5)", "main", 7, [
            ("h1_minus_1.5", "Фора 1 (-1.5)", odd_h1_minus),
            ("h2_plus_1.5", "Фора 2 (+1.5)", odd_h2_plus),
            ("h1_plus_1.5", "Фора 1 (+1.5)", odd_h1_plus),
            ("h2_minus_1.5", "Фора 2 (-1.5)", odd_h2_minus),
        ]),
    ]

    return apply_market_spec(match_id, spec)


# ─── Общий кубок: роспись этапа ───────────────────────────────────────────────
# Кубковые рынки пишет та же функция, что и лиговые (`apply_market_spec`) —
# различается только спека. Из неё намеренно убраны `X`, `1X`, `X2` и `12`:
# ничьей в кубке нет (её разрешают послематчевые), а «П1 или П2» при
# гарантированном победителе наступал бы всегда и при марже 7.5% был бы подарком
# игроку. Живут кубковые рынки только в реляционной схеме — `bet_markets.odd_x`
# это NOT NULL, и выдуманное число туда класть нечем.
#
# Считается цена в `services/cup_strength.py`: сила клуба = сила в СВОЁМ
# дивизионе + надбавка за класс, без преимущества поля.


def _cup_match_spec(team1_name: str, team2_name: str, o: dict) -> list:
    """Роспись одной игры серии.

    `1x2` здесь — победитель ИГРЫ с учётом послематчевых (в кубке их не бывает
    ничьей), поэтому в названии рынка это сказано: calculate/послематчевые
    различаются для игрока, когда счёт основного времени 2:2. Тоталы, ОЗ,
    индивидуальные тоталы и фора считаются по основному времени: «ТМ2.5»
    проигрывает при 2:2 независимо от того, кто взял игру.
    """
    return [
        ("1x2", "Исход матча (с послематчевыми)", "main", 1, [
            ("p1", f"П1 ({team1_name})", o["p1"]),
            ("p2", f"П2 ({team2_name})", o["p2"]),
        ]),
        ("total_goals", "Тотал голов", "goals", 3, [
            ("over_1.5", "Тотал больше (1.5)", o["tb15"]),
            ("under_1.5", "Тотал меньше (1.5)", o["tm15"]),
            ("over_2.5", "Тотал больше (2.5)", o["tb25"]),
            ("under_2.5", "Тотал меньше (2.5)", o["tm25"]),
            ("over_3.5", "Тотал больше (3.5)", o["tb35"]),
            ("under_3.5", "Тотал меньше (3.5)", o["tm35"]),
        ]),
        ("btts", "Обе забьют", "goals", 4, [
            ("btts_yes", "Обе забьют: Да", o["btts_yes"]),
            ("btts_no", "Обе забьют: Нет", o["btts_no"]),
        ]),
        ("individual_total_1", f"Инд. тотал: {team1_name}", "goals", 5, [
            ("it1_over_1.5", "ИТБ1 (1.5)", o["it1_over_1.5"]),
            ("it1_under_1.5", "ИТМ1 (1.5)", o["it1_under_1.5"]),
        ]),
        ("individual_total_2", f"Инд. тотал: {team2_name}", "goals", 6, [
            ("it2_over_1.5", "ИТБ2 (1.5)", o["it2_over_1.5"]),
            ("it2_under_1.5", "ИТМ2 (1.5)", o["it2_under_1.5"]),
        ]),
        ("handicap", "Фора (1.5)", "main", 7, [
            ("h1_minus_1.5", "Фора 1 (-1.5)", o["h1_minus_1.5"]),
            ("h2_plus_1.5", "Фора 2 (+1.5)", o["h2_plus_1.5"]),
            ("h1_plus_1.5", "Фора 1 (+1.5)", o["h1_plus_1.5"]),
            ("h2_minus_1.5", "Фора 2 (-1.5)", o["h2_minus_1.5"]),
        ]),
    ]


def generate_cup_match_markets(
    match_id: int,
    team1_name: str,
    team2_name: str,
    season_id: Optional[int] = None
) -> list[dict]:
    """Выставить одну игру серии общего кубка."""
    from services.cup_strength import cup_match_odds

    priced = cup_match_odds(team1_name, team2_name, season_id=season_id)
    return apply_market_spec(match_id, _cup_match_spec(team1_name, team2_name, priced["odds"]))


def _cup_series_spec(team1_name: str, team2_name: str, o: dict) -> list:
    """Роспись серии целиком на её строке-заголовке.

    «Счёт» заголовка — победы в серии, поэтому все три рынка обслуживает
    действующий `market_settler` без новых правил: `1x2` сравнивает число побед
    («кто проходит»), `correct_score` уже умеет `cs_2_0`..`cs_0_2`, а «будет ли
    третья игра» — обычный `total_goals` с линией 2.5 поверх счёта серии.
    """
    return [
        ("1x2", "Кто проходит дальше", "main", 1, [
            ("p1", f"Проход ({team1_name})", o["p1"]),
            ("p2", f"Проход ({team2_name})", o["p2"]),
        ]),
        ("correct_score", "Счёт серии", "main", 2, [
            ("cs_2_0", "2:0", o["series_2_0"]),
            ("cs_2_1", "2:1", o["series_2_1"]),
            ("cs_1_2", "1:2", o["series_1_2"]),
            ("cs_0_2", "0:2", o["series_0_2"]),
        ]),
        ("total_goals", "Тотал игр в серии", "goals", 3, [
            ("over_2.5", "Третья игра будет", o["over_2.5"]),
            ("under_2.5", "Третья игра не нужна", o["under_2.5"]),
        ]),
    ]


def generate_cup_series_markets(
    match_id: int,
    team1_name: str,
    team2_name: str,
    season_id: Optional[int] = None
) -> list[dict]:
    """Выставить серию общего кубка на её строке-заголовке.

    `match_id` — id строки `matches` с `is_series_header = 1`, имена клубов
    приходят из `cup_series` (у заголовка своих имён нет намеренно).
    """
    from services.cup_strength import best_of_three_odds

    priced = best_of_three_odds(team1_name, team2_name, season_id=season_id)
    return apply_market_spec(match_id, _cup_series_spec(team1_name, team2_name, priced["odds"]))
