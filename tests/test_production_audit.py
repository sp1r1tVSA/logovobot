import asyncio
import itertools
import os
import re
import ast
import time
import hmac
import hashlib
import urllib.parse
import json
import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
import database
import config
from api.auth import validate_telegram_init_data, get_authenticated_user
from api.routes_matches import handle_get_matches
from api.routes_markets import handle_get_match_markets
import services.odds_engine as odds_engine
from services.cashout_engine import execute_cashout
from handlers.base import is_global_admin, is_admin


def test_odds_consistency_api_matches_vs_markets():
    """Verify that GET /api/matches and GET /api/matches/{id}/markets return identical canonical odds."""
    async def _run():
        test_uid = 999101
        m_id = 8801

        # Setup test data in DB
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))
            cursor.execute("DELETE FROM bet_markets WHERE match_id = ?", (m_id,))
            cursor.execute("DELETE FROM markets WHERE match_id = ?", (m_id,))
            cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))

            cursor.execute("""
                INSERT INTO users (telegram_id, username, role)
                VALUES (?, 'test_odds_user', 'admin')
            """, (test_uid,))

            cursor.execute("""
                INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id, season_id)
                VALUES (?, 1, 'Arsenal', 'Chelsea', 'scheduled', 1, 1)
            """, (m_id,))

        # 1. Generate markets via odds_engine
        markets = odds_engine.generate_match_markets(m_id, "Arsenal", "Chelsea")
        assert len(markets) > 0

        # 2. Update odds for p1 to a distinct custom value (2.75) via set_odds
        m_1x2 = [m for m in markets if m["market_key"] == "1x2"][0]
        odds_engine.set_odds(m_1x2["id"], "p1", 2.75, admin_id=test_uid, reason="Sharp money adjustment")

        # 3. Request GET /api/matches
        req_matches = make_mocked_request(
            "GET",
            f"/api/matches?division_id=1",
            headers={"X-Telegram-Init-Data": f"mock_admin_{test_uid}"}
        )
        os.environ["ALLOW_DEV_AUTH_BYPASS"] = "1"
        res_matches = await handle_get_matches(req_matches)
        assert res_matches.status == 200
        body_matches = json.loads(res_matches.text)
        assert body_matches["status"] == "ok"
        target_match = [m for m in body_matches["matches"] if m["id"] == m_id][0]
        p1_odd_matches = target_match["odds"]["p1"]

        # 4. Request GET /api/matches/{id}/markets
        req_markets = make_mocked_request(
            "GET",
            f"/api/matches/{m_id}/markets",
            headers={"X-Telegram-Init-Data": f"mock_admin_{test_uid}"}
        )
        req_markets.match_info["id"] = str(m_id)
        res_markets = await handle_get_match_markets(req_markets)
        assert res_markets.status == 200
        body_markets = json.loads(res_markets.text)
        assert body_markets["status"] == "ok"

        mkt_1x2 = [m for m in body_markets["markets"] if m["market_key"] == "1x2"][0]
        sel_p1 = [s for s in mkt_1x2["selections"] if s["selection_key"] == "p1"][0]
        p1_odd_markets = sel_p1.get("current_odd") or sel_p1.get("odds_value")

        # ASSERT CANONICAL CONSISTENCY: both must return 2.75
        assert p1_odd_matches == 2.75
        assert p1_odd_markets == 2.75
        assert p1_odd_matches == p1_odd_markets

        # Clean up
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))
            cursor.execute("DELETE FROM bet_markets WHERE match_id = ?", (m_id,))
            cursor.execute("DELETE FROM markets WHERE match_id = ?", (m_id,))
            cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))

    asyncio.run(_run())


def test_place_user_bet_no_double_debit_on_idempotency_conflict():
    """Verify that when duplicate idempotency key is submitted, user wallet is NOT debited twice."""
    test_uid = 999102
    m_id = 8802

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_bets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))

        cursor.execute("""
            INSERT INTO users (telegram_id, username, role)
            VALUES (?, 'test_idemp_user', 'player')
        """, (test_uid,))
        cursor.execute("""
            INSERT INTO user_wallets (user_id, balance, total_wagered)
            VALUES (?, 1000, 0)
        """, (test_uid,))
        cursor.execute("""
            INSERT INTO matches (id, round_number, player1_team, player2_team, status, division_id, season_id)
            VALUES (?, 1, 'Arsenal', 'Chelsea', 'scheduled', 1, 1)
        """, (m_id,))
        cursor.execute("""
            INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active)
            VALUES (?, 1, 'Arsenal', 'Chelsea', 2.0, 3.0, 3.5, 1)
        """, (m_id,))
        # Приём ставок разрешён только при открытой линии на ещё не открытом туре.
        cursor.execute("""
            INSERT OR REPLACE INTO rounds (round_number, division_id, season_id, is_open, bets_open)
            VALUES (1, 1, 1, 0, 1)
        """)

    # First bet placement
    idemp_key = "unique-key-102"
    selections = [{"match_id": m_id, "outcome": "p1", "odd": 2.0}]
    ok1, bet_id1 = database.place_user_bet(
        user_id=test_uid,
        amount=200,
        selections=selections,
        idempotency_key=idemp_key
    )
    assert ok1 is True
    assert isinstance(bet_id1, int)

    bal1 = database.get_wallet_balance(test_uid)
    assert bal1 == 800  # 1000 - 200

    # Second bet placement with EXACT same idempotency key
    ok2, bet_id2 = database.place_user_bet(
        user_id=test_uid,
        amount=200,
        selections=selections,
        idempotency_key=idemp_key
    )
    assert ok2 is True
    assert bet_id2 == bet_id1

    # CRITICAL INVARIANT: balance must still be exactly 800, NOT 600
    bal2 = database.get_wallet_balance(test_uid)
    assert bal2 == 800

    # Clean up
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_bets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))
        cursor.execute("DELETE FROM bet_markets WHERE match_id = ?", (m_id,))


def test_cashout_idempotency_and_no_double_payout():
    """Verify that multiple cashout attempts cannot trigger double payout."""
    test_uid = 999103
    m_id = 8803

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_bets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))

        cursor.execute("INSERT INTO users (telegram_id, username) VALUES (?, 'cash_u')", (test_uid,))
        cursor.execute("INSERT INTO user_wallets (user_id, balance) VALUES (?, 1000)", (test_uid,))
        cursor.execute("INSERT INTO matches (id, round_number, player1_team, player2_team, status) VALUES (?, 1, 'T1', 'T2', 'scheduled')", (m_id,))
        cursor.execute("INSERT INTO bet_markets (match_id, tour, team1_name, team2_name, odd_p1, odd_x, odd_p2, is_active) VALUES (?, 1, 'T1', 'T2', 2.0, 3.0, 3.5, 1)", (m_id,))
        # Приём ставок разрешён только при открытой линии на ещё не открытом туре.
        cursor.execute("INSERT OR REPLACE INTO rounds (round_number, division_id, season_id, is_open, bets_open) VALUES (1, 1, 1, 0, 1)")

    # Place bet
    ok, bet_id = database.place_user_bet(
        user_id=test_uid,
        amount=500,
        selections=[{"match_id": m_id, "outcome": "p1", "odd": 2.0}]
    )
    assert ok is True

    bal_after_bet = database.get_wallet_balance(test_uid)
    assert bal_after_bet == 500

    # Execute Cashout #1
    success1, res1 = execute_cashout(user_id=test_uid, bet_id=bet_id)
    assert success1 is True
    payout1 = res1["cashout_payout"]
    assert payout1 > 0

    bal_after_cashout = database.get_wallet_balance(test_uid)
    assert bal_after_cashout == 500 + payout1

    # Execute Cashout #2 (Repeated / Concurrent Attempt)
    success2, res2 = execute_cashout(user_id=test_uid, bet_id=bet_id)
    assert success2 is False  # Must be rejected because bet is already settled
    assert res2.get("error") == "ALREADY_SETTLED"

    # Wallet balance MUST NOT increase again
    final_bal = database.get_wallet_balance(test_uid)
    assert final_bal == 500 + payout1

    # Clean up
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_wallets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM user_bets WHERE user_id = ?", (test_uid,))
        cursor.execute("DELETE FROM matches WHERE id = ?", (m_id,))
        cursor.execute("DELETE FROM bet_markets WHERE match_id = ?", (m_id,))


def test_standings_division_isolation():
    """Verify that matches from Division 2 NEVER leak into Division 1 / Global KPL standings."""
    m1_id = 8811
    m2_id = 8812

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM matches WHERE id IN (?, ?)", (m1_id, m2_id))
        # Match in Division 1
        cursor.execute("""
            INSERT INTO matches (id, round_number, player1_team, player2_team, player1_score, player2_score, status, division_id, season_id)
            VALUES (?, 1, 'Бенфика', 'Аякс', 3, 1, 'confirmed', 1, 1)
        """, (m1_id,))
        # Match in Division 2 (same team names or different)
        cursor.execute("""
            INSERT INTO matches (id, round_number, player1_team, player2_team, player1_score, player2_score, status, division_id, season_id)
            VALUES (?, 1, 'Бенфика', 'Аякс', 0, 5, 'confirmed', 2, 1)
        """, (m2_id,))

    # 1. Standings for Division 1
    st_div1 = database.get_standings(division_id=1, season_id=1)
    benfica_div1 = [t for t in st_div1 if t["team_name"] == "Бенфика"][0]
    assert benfica_div1["played"] == 1
    assert benfica_div1["wins"] == 1
    assert benfica_div1["goals_scored"] == 3
    assert benfica_div1["goals_conceded"] == 1

    # 2. Standings for Global / KPL (division_id=None)
    st_global = database.get_standings(division_id=None, season_id=1)
    benfica_global = [t for t in st_global if t["team_name"] == "Бенфика"][0]
    # In Global KPL, Division 2 match MUST NOT count!
    assert benfica_global["played"] == 1
    assert benfica_global["wins"] == 1
    assert benfica_global["goals_scored"] == 3
    assert benfica_global["goals_conceded"] == 1

    # Clean up
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM matches WHERE id IN (?, ?)", (m1_id, m2_id))


def test_telegram_webapp_auth_validation():
    """Verify cryptographic HMAC-SHA256 signature verification, freshness and expiration."""
    test_token = "123456789:ABCdefGHIjklMNOpqrSTUvwxYZ_01234567"
    secret_key = hmac.new(b"WebAppData", test_token.encode("utf-8"), hashlib.sha256).digest()

    now = int(time.time())
    user_payload = {"id": 555777, "first_name": "Authentic", "username": "auth_user"}
    user_str = json.dumps(user_payload, separators=(',', ':'))

    # 1. Build valid initData
    params = {
        "auth_date": str(now - 60),  # 1 min ago
        "query_id": "AAHdF6IQAAAAAN0XohD9p",
        "user": user_str
    }
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    valid_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    params["hash"] = valid_hash

    init_data_valid = urllib.parse.urlencode(params)
    parsed_user = validate_telegram_init_data(init_data_valid, bot_token=test_token)
    assert parsed_user is not None
    assert parsed_user["id"] == 555777
    assert parsed_user["first_name"] == "Authentic"

    # 2. Tampered hash -> must fail
    params_tampered = dict(params)
    params_tampered["hash"] = "deadbeef" + valid_hash[8:]
    assert validate_telegram_init_data(urllib.parse.urlencode(params_tampered), bot_token=test_token) is None

    # 3. Expired auth_date (> 24 hours ago) -> must fail
    params_expired = dict(params)
    params_expired["auth_date"] = str(now - 100000)
    data_check_exp = "\n".join(f"{k}={v}" for k, v in sorted(params_expired.items()) if k != "hash")
    params_expired["hash"] = hmac.new(secret_key, data_check_exp.encode("utf-8"), hashlib.sha256).hexdigest()
    assert validate_telegram_init_data(urllib.parse.urlencode(params_expired), bot_token=test_token) is None

    # 4. Far future auth_date (> 5 min ahead) -> must fail
    params_future = dict(params)
    params_future["auth_date"] = str(now + 1000)
    data_check_fut = "\n".join(f"{k}={v}" for k, v in sorted(params_future.items()) if k != "hash")
    params_future["hash"] = hmac.new(secret_key, data_check_fut.encode("utf-8"), hashlib.sha256).hexdigest()
    assert validate_telegram_init_data(urllib.parse.urlencode(params_future), bot_token=test_token) is None


def test_admin_rbac_isolation_division_vs_global():
    """Verify that division admin is isolated and cannot perform global actions."""
    div_admin_id = 777111
    global_admin_id = 777222

    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (div_admin_id, global_admin_id))
        cursor.execute("DELETE FROM division_admins WHERE user_id IN (?, ?)", (div_admin_id, global_admin_id))

        # Division Admin: assigned role division_admin and tied to division 2
        cursor.execute("""
            INSERT INTO users (telegram_id, username, role, division_id)
            VALUES (?, 'div_admin', 'division_admin', 2)
        """, (div_admin_id,))
        cursor.execute("INSERT INTO division_admins (division_id, user_id) VALUES (2, ?)", (div_admin_id,))

        # Global Admin: role admin, no division
        cursor.execute("""
            INSERT INTO users (telegram_id, username, role, division_id)
            VALUES (?, 'global_admin', 'admin', NULL)
        """, (global_admin_id,))

    assert is_admin(div_admin_id) is True  # is_admin returns True
    assert is_global_admin(div_admin_id) is False  # is_global_admin MUST return False!

    assert is_admin(global_admin_id) is True
    assert is_global_admin(global_admin_id) is True

    # Clean up
    with database.transaction() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE telegram_id IN (?, ?)", (div_admin_id, global_admin_id))
        cursor.execute("DELETE FROM division_admins WHERE user_id IN (?, ?)", (div_admin_id, global_admin_id))


# ---------------------------------------------------------------------------
# Аудит inline-кнопок
#
# Каждая кнопка в handlers/ обязана попадать в живой CallbackQueryHandler.
# Разбор идёт по AST, а не построчно: построчный сканер спотыкается о
# многострочный вызов InlineKeyboardButton и не умеет ходить за
# `callback_data=помощник(...)`, из-за чего требует подгонять форматирование
# кода под парсер и держать список хардкоженных «образцов».
#
# Идея: каждое выражение callback_data сворачивается в список вариантов —
# кусочков строк вперемешку с «дырками» (_Hole) там, где значение известно
# только в рантайме. Дырка заполняется заглушками, и получившаяся проба
# проверяется против скомпилированных паттернов ровно так, как это делает
# python-telegram-bot: `pattern.match(callback_data)`.
# ---------------------------------------------------------------------------

_AUDIT_MAX_VARIANTS = 12   # сколько веток одного выражения разворачиваем
_AUDIT_MAX_PROBES = 5000   # потолок перебора заглушек на одну кнопку
_AUDIT_MAX_DEPTH = 8       # глубина раскрутки имён и вызовов
_AUDIT_FILLERS = ("1", "-1", "x")

# Кнопки, чей callback_data физически не виден в AST: значение кладут в
# user_data на одном экране, а рисуют на другом. Они проверяются по месту
# производства, а здесь список держится закрытым — чтобы новая такая кнопка
# требовала осознанного решения, а не проскакивала молча.
_AUDIT_OPAQUE_ALLOWLIST = {
    ("admin", "back_cb"),     # context.user_data["admin_player_back_cb"]
    ("squad_ai", "back_cb"),  # pending["back_cb"] из ожидающего разбора состава
    ("cabinet", "confirm_cb"),  # кубок: кнопка, что привела к выбору победителя (cb_confirm_ai_final_/cb_submit_report_to_guest_)
}

_AUDIT_ALT_TOKEN = re.compile(r"[\w/-]{1,16}")


class _Hole:
    """Рантайм-значение внутри callback_data: `{div_id}` в f-строке."""

    __slots__ = ("hint",)

    def __init__(self, hint):
        self.hint = hint

    def __repr__(self):
        return "{" + self.hint + "}"


class _AuditIndex:
    """Всё, что нужно для резолва: константы, функции и их вызовы."""

    def __init__(self):
        self.trees = {}          # module -> ast.Module
        self.consts = {}         # NAME -> [выражения] для module-level присваиваний
        self.functions = {}      # (module, func) -> def
        self.callsite_args = {}  # (func, param) -> [выражения из вызовов]
        self.owner = {}          # id(node) -> (module, ближайший FunctionDef)
        self._scopes = {}

    def load_path(self, path):
        with open(path, "r", encoding="utf-8") as f:
            self.load_source(os.path.splitext(os.path.basename(path))[0], f.read())

    def load_source(self, module, source):
        tree = ast.parse(source)
        self.trees[module] = tree

        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        self.consts.setdefault(target.id, []).append(node.value)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.functions[(module, node.name)] = node

        self.owner[id(tree)] = (module, None)
        self._index_owners(tree, module, None)

        # Параметр функции-рендерера резолвится через то, что в него передают на
        # вызовах: `_render(update, back_cb="admin_divs_hub")`.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if not name:
                continue
            for kw in node.keywords:
                if kw.arg:
                    self.callsite_args.setdefault((name, kw.arg), []).append(kw.value)

    def _index_owners(self, node, module, func):
        for child in ast.iter_child_nodes(node):
            nxt = child if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            self.owner[id(child)] = (module, nxt)
            self._index_owners(child, module, nxt)

    def func_of(self, node, module):
        return self.owner.get(id(node), (module, None))[1]

    def scope(self, func):
        """Локальные привязки функции: значения по умолчанию и все присваивания."""
        if func is None:
            return {}
        cached = self._scopes.get(id(func))
        if cached is not None:
            return cached

        scope = {}
        args = func.args
        for arg, default in zip(reversed(args.posonlyargs + args.args), reversed(args.defaults)):
            scope.setdefault(arg.arg, []).append(default)
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None:
                scope.setdefault(arg.arg, []).append(default)
        for node in ast.walk(func):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        scope.setdefault(target.id, []).append(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value:
                scope.setdefault(node.target.id, []).append(node.value)

        self._scopes[id(func)] = scope
        return scope


def _audit_union(left, right):
    if left is None:
        return right
    if right is None:
        return left
    return (left + [v for v in right if v not in left])[:_AUDIT_MAX_VARIANTS]


def _audit_resolve(node, index, func, depth=0):
    """Выражение → список вариантов callback_data (строки вперемешку с дырками).

    None означает «непрозрачно»: значение приходит извне AST.
    """
    if depth > _AUDIT_MAX_DEPTH:
        return None

    if isinstance(node, ast.Constant):
        return [[node.value]] if isinstance(node.value, str) else None

    if isinstance(node, ast.JoinedStr):
        variants = [[]]
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                variants = [v + [part.value] for v in variants]
            elif isinstance(part, ast.FormattedValue):
                inner = _audit_resolve(part.value, index, func, depth + 1)
                if inner is None:
                    hole = _Hole(ast.unparse(part.value))
                    variants = [v + [hole] for v in variants]
                else:
                    variants = [v + iv for v in variants for iv in inner][:_AUDIT_MAX_VARIANTS]
            else:
                return None
        return variants

    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _audit_resolve(node.left, index, func, depth + 1)
        right = _audit_resolve(node.right, index, func, depth + 1)
        if left is None or right is None:
            return None
        return [a + b for a in left for b in right][:_AUDIT_MAX_VARIANTS]

    # `x if cond else y` и `a or b` дают обе ветки — кнопка обязана быть живой
    # в каждой из них.
    if isinstance(node, ast.IfExp):
        return _audit_union(
            _audit_resolve(node.body, index, func, depth + 1),
            _audit_resolve(node.orelse, index, func, depth + 1),
        )

    if isinstance(node, ast.BoolOp):
        out = None
        for value in node.values:
            out = _audit_union(out, _audit_resolve(value, index, func, depth + 1))
        return out

    if isinstance(node, ast.Name):
        bindings = index.scope(func).get(node.id) or index.consts.get(node.id)
        if bindings is None and func is not None:
            params = {a.arg for a in func.args.posonlyargs + func.args.args + func.args.kwonlyargs}
            if node.id in params:
                bindings = index.callsite_args.get((func.name, node.id))
        if not bindings:
            return None
        out = None
        for binding in bindings:
            if binding is node:
                continue
            out = _audit_union(out, _audit_resolve(binding, index, func, depth + 1))
        return out

    # `callback_data=_div_home_cb(update, div_id)` — объединение всех return'ов.
    if isinstance(node, ast.Call):
        name = getattr(node.func, "id", None)
        if name:
            for (_, fname), fn in index.functions.items():
                if fname != name:
                    continue
                out = None
                for inner in ast.walk(fn):
                    if isinstance(inner, ast.Return) and inner.value is not None:
                        out = _audit_union(out, _audit_resolve(inner.value, index, fn, depth + 1))
                if out:
                    return out
        return None

    return None


def _audit_harvest_tokens(patterns):
    """Словарь литералов, которые хендлеры реально принимают.

    Дырка вроде `{short_code}` не обязана быть числом: паттерн
    `^reassign_top:\\d+:(d|p|r|rep|l|a)$` перечисляет допустимые значения сам.
    Собираем их из альтернатив в группах — это избавляет от угадывания
    заглушек по имени переменной.
    """
    tokens = set()
    for pattern in patterns:
        for group in re.findall(r"\(([^()]*)\)", pattern.pattern):
            group = group.lstrip("?:").lstrip("?")
            if "|" not in group:
                continue
            for alt in group.split("|"):
                if _AUDIT_ALT_TOKEN.fullmatch(alt):
                    tokens.add(alt)
    return tuple(sorted(tokens))


def _audit_probes(variant, tokens):
    """Вариант с дырками → конкретные строки-пробы.

    Сначала дешёвый прогон на числовых заглушках — он закрывает подавляющее
    большинство callback_data. Второй проход подставляет словарь, собранный из
    самих паттернов, и нужен считанным кнопкам.
    """
    if all(isinstance(segment, str) for segment in variant):
        yield "".join(variant)
        return
    for fillers in (_AUDIT_FILLERS, _AUDIT_FILLERS + tokens):
        choices = [[s] if isinstance(s, str) else list(fillers) for s in variant]
        for combo in itertools.islice(itertools.product(*choices), _AUDIT_MAX_PROBES):
            yield "".join(combo)


def _audit_patterns(index):
    """Скомпилированные паттерны всех зарегистрированных CallbackQueryHandler."""
    patterns = []
    for module, tree in index.trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "CallbackQueryHandler":
                continue
            source = next((kw.value for kw in node.keywords if kw.arg == "pattern"), None)
            if source is None and len(node.args) >= 2:
                source = node.args[1]
            if source is None:
                continue
            variants = _audit_resolve(source, index, index.func_of(source, module)) or []
            for variant in variants:
                if any(isinstance(s, _Hole) for s in variant):
                    continue
                pattern = "".join(variant)
                # Catch-all намеренно не считается регистрацией: он ловит всё
                # подряд и сделал бы аудит бессмысленным.
                if pattern in (".*", "^.*$"):
                    continue
                try:
                    patterns.append(re.compile(pattern))
                except re.error:
                    pass
    return patterns


def _audit_literal_dispatch(index):
    """callback_data, разбираемые сравнением `query.data == "literal"`.

    Catch-all хендлер за регистрацию не считается, но внутри него конкретные
    значения перечислены явно — они и есть настоящий контракт (так живёт
    `noop` в handle_placeholders).
    """
    handled = set()
    for module, tree in index.trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "CallbackQueryHandler":
                continue
            target = node.args[0] if node.args else None
            fn = index.functions.get((module, getattr(target, "id", "")))
            if fn is None:
                continue
            for cmp_node in ast.walk(fn):
                if not isinstance(cmp_node, ast.Compare):
                    continue
                if not (isinstance(cmp_node.left, ast.Attribute) and cmp_node.left.attr == "data"):
                    continue
                for op, other in zip(cmp_node.ops, cmp_node.comparators):
                    if isinstance(op, ast.Eq):
                        other = [other]
                    elif isinstance(op, ast.In) and isinstance(other, (ast.Tuple, ast.List, ast.Set)):
                        other = other.elts
                    else:
                        continue
                    for elt in other:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            handled.add(elt.value)
    return handled


def _audit_buttons(index):
    """Все InlineKeyboardButton, кроме url/web_app — у тех callback_data нет."""
    for module, tree in index.trees.items():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "InlineKeyboardButton":
                continue
            keywords = {kw.arg: kw.value for kw in node.keywords}
            if "url" in keywords or "web_app" in keywords:
                continue
            callback = keywords.get("callback_data")
            if callback is None and len(node.args) >= 2:
                callback = node.args[1]
            yield module, node, callback


def _audit_run(index):
    """→ (осиротевшие кнопки, непрозрачные кнопки)."""
    patterns = _audit_patterns(index)
    literals = _audit_literal_dispatch(index)
    tokens = _audit_harvest_tokens(patterns)

    unmatched, opaque = [], []
    for module, node, callback in _audit_buttons(index):
        where = f"{module}.py:{node.lineno}"
        if callback is None:
            unmatched.append((where, "callback_data отсутствует"))
            continue

        variants = _audit_resolve(callback, index, index.func_of(callback, module))
        if not variants:
            opaque.append((module, ast.unparse(callback)))
            continue

        for variant in variants:
            first = None
            for probe in _audit_probes(variant, tokens):
                first = probe if first is None else first
                if probe in literals or any(r.match(probe) for r in patterns):
                    break
            else:
                unmatched.append((where, first))

    return unmatched, opaque


def _audit_index_of_handlers():
    workspace = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    index = _AuditIndex()
    const_path = os.path.join(workspace, "constants.py")
    if os.path.exists(const_path):
        index.load_path(const_path)
    handlers_dir = os.path.join(workspace, "handlers")
    for fname in sorted(os.listdir(handlers_dir)):
        if fname.endswith(".py"):
            index.load_path(os.path.join(handlers_dir, fname))
    return index


def test_all_inline_buttons_match_registered_handlers():
    """Каждая inline-кнопка в handlers/ ведёт к зарегистрированному хендлеру."""
    unmatched, opaque = _audit_run(_audit_index_of_handlers())

    assert unmatched == [], f"Кнопки без хендлера: {unmatched}"

    # Непрозрачных кнопок ровно столько, сколько задокументировано выше.
    assert set(opaque) == _AUDIT_OPAQUE_ALLOWLIST, (
        f"Изменился набор кнопок с непрозрачным callback_data: {sorted(set(opaque))}"
    )


def test_button_audit_detects_an_orphan_button():
    """Аудит обязан падать на осиротевшей кнопке — иначе он ничего не стоит."""
    index = _AuditIndex()
    index.load_source("fake", (
        "def register(app):\n"
        "    app.add_handler(CallbackQueryHandler(live, pattern=r'^live:\\d+$'))\n"
        "\n"
        "def screen(div_id):\n"
        "    return [\n"
        "        InlineKeyboardButton('живая', callback_data=f'live:{div_id}'),\n"
        "        InlineKeyboardButton('мёртвая', callback_data=f'dead:{div_id}'),\n"
        "    ]\n"
    ))

    unmatched, opaque = _audit_run(index)

    assert opaque == []
    assert [probe for _, probe in unmatched] == ["dead:1"]
