"""
tests/test_orphan_open_markets.py

Рынки «Открыт» у матчей с закрытой линией (админ-панель «Рынки»):

  * `GET /api/matches/{id}/markets` не строит роспись «на лету» для матча,
    чья линия закрыта, — раньше такой просмотр оставлял рынки `open`;
  * `database.close_orphan_open_markets` гасит уже накопившиеся рынки.
"""

import asyncio
import json
import os
import unittest

from aiohttp.test_utils import make_mocked_request

import database
from api.routes_markets import handle_get_match_markets
from services import odds_engine

DIV = 1
SEASON = 1
USER_ID = 998801

R_LINE_OPEN = 281    # is_open=0, bets_open=1 — линия открыта
R_NO_LINE = 282      # is_open=0, bets_open=0 — тур ещё впереди, линия закрыта
R_PLAYING = 283      # is_open=1, bets_open=0 — тур играется

M_LINE_OPEN = 99851
M_NO_LINE = 99852
M_PLAYING = 99853
M_PLAYED = 99854     # сыгран: рассчитанные рынки не трогаем
M_NO_ROUND = 99855   # строки тура нет вовсе

M_IDS = (M_LINE_OPEN, M_NO_LINE, M_PLAYING, M_PLAYED, M_NO_ROUND)


def _statuses(match_id):
    with database.transaction() as conn:
        rows = conn.execute("SELECT DISTINCT status FROM markets WHERE match_id = ?", (match_id,)).fetchall()
    return {r["status"] for r in rows}


class TestOrphanOpenMarkets(unittest.TestCase):
    def setUp(self):
        database.init_db()
        self._cleanup()
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT OR REPLACE INTO users (telegram_id, username, role) VALUES (?, 'orphan_admin', 'admin')",
                (USER_ID,),
            )
            for rn, is_open, bets_open in (
                (R_LINE_OPEN, 0, 1), (R_NO_LINE, 0, 0), (R_PLAYING, 1, 0),
            ):
                c.execute(
                    "INSERT INTO rounds (round_number, is_open, bets_open, deadline, division_id, season_id) "
                    "VALUES (?, ?, ?, NULL, ?, ?)",
                    (rn, is_open, bets_open, DIV, SEASON),
                )
            for m_id, rn, status in (
                (M_LINE_OPEN, R_LINE_OPEN, "pending"),
                (M_NO_LINE, R_NO_LINE, "pending"),
                (M_PLAYING, R_PLAYING, "scheduled"),
                (M_PLAYED, R_NO_LINE, "confirmed"),
                (M_NO_ROUND, 289, "pending"),
            ):
                c.execute(
                    "INSERT INTO matches (id, round_number, division_id, season_id, "
                    "player1_team, player2_team, status) VALUES (?, ?, ?, ?, 'Orphan A', 'Orphan B', ?)",
                    (m_id, rn, DIV, SEASON, status),
                )
        for m_id in M_IDS:
            odds_engine.generate_match_markets(m_id, "Orphan A", "Orphan B")

    def tearDown(self):
        self._cleanup()

    def _cleanup(self):
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "DELETE FROM market_selections WHERE market_id IN "
                "(SELECT id FROM markets WHERE match_id >= 99850 AND match_id < 99860)"
            )
            c.execute("DELETE FROM markets WHERE match_id >= 99850 AND match_id < 99860")
            c.execute("DELETE FROM bet_markets WHERE match_id >= 99850 AND match_id < 99860")
            c.execute("DELETE FROM matches WHERE id >= 99850 AND id < 99860")
            c.execute("DELETE FROM rounds WHERE round_number BETWEEN 281 AND 289")
            c.execute("DELETE FROM users WHERE telegram_id = ?", (USER_ID,))

    def _drop_markets(self, match_id):
        with database.transaction() as conn:
            conn.execute(
                "DELETE FROM market_selections WHERE market_id IN (SELECT id FROM markets WHERE match_id = ?)",
                (match_id,),
            )
            conn.execute("DELETE FROM markets WHERE match_id = ?", (match_id,))

    def _get_markets(self, match_id):
        async def _run():
            os.environ["ALLOW_DEV_AUTH_BYPASS"] = "1"
            req = make_mocked_request(
                "GET", f"/api/matches/{match_id}/markets",
                headers={"X-Telegram-Init-Data": f"mock_admin_{USER_ID}"},
            )
            req.match_info["id"] = str(match_id)
            res = await handle_get_match_markets(req)
            self.assertEqual(res.status, 200)
            return json.loads(res.text)["markets"]
        return asyncio.run(_run())

    # --- API: роспись на лету только при открытой линии -------------------
    def test_01_view_does_not_generate_for_closed_line(self):
        self._drop_markets(M_NO_LINE)
        self.assertEqual(self._get_markets(M_NO_LINE), [])
        self.assertEqual(_statuses(M_NO_LINE), set())

    def test_02_view_generates_for_open_line(self):
        self._drop_markets(M_LINE_OPEN)
        markets = self._get_markets(M_LINE_OPEN)
        self.assertTrue(markets)
        self.assertEqual(_statuses(M_LINE_OPEN), {"open"})

    # --- repair ---------------------------------------------------------
    def test_03_repair_closes_only_closed_line_matches(self):
        # Играемый матч и матч без тура закрыты, у сыгранного рынки уже «живые» по сетапу.
        closed = database.close_orphan_open_markets()
        self.assertGreater(closed, 0)

        self.assertEqual(_statuses(M_NO_LINE), {"closed"})
        self.assertEqual(_statuses(M_PLAYING), {"closed"})
        self.assertEqual(_statuses(M_NO_ROUND), {"closed"})
        # Открытая линия и сыгранный матч не тронуты.
        self.assertEqual(_statuses(M_LINE_OPEN), {"open"})
        self.assertEqual(_statuses(M_PLAYED), {"open"})

        with database.transaction() as conn:
            active = conn.execute(
                "SELECT COUNT(*) AS c FROM market_selections WHERE status = 'active' AND market_id IN "
                "(SELECT id FROM markets WHERE match_id = ?)", (M_NO_LINE,)
            ).fetchone()["c"]
        self.assertEqual(active, 0)

    def test_04_repair_is_idempotent(self):
        database.close_orphan_open_markets()
        self.assertEqual(database.close_orphan_open_markets(), 0)


if __name__ == "__main__":
    unittest.main()
