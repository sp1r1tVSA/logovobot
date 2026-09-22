"""
tests/test_cup_series_progression.py

Результат кубковой игры → движение серии → расчёт ставок.

Здесь сосредоточены три правила, каждое из которых наедине с собой тихое.

Победитель игры не выводится из счёта. Ничьих в кубке нет, 2:2 в основное время
разрешается послематчевыми, и на скриншоте статистики их не видно. Значит у
равного счёта обязан быть объявленный клуб (`matches.cup_winner_team`), а
подтверждение без него — откатываться целиком: подтверждённая игра с незакрытой
серией — это сетка, в которой прошёл не тот клуб.

Серия, закончившаяся 2:0, обязана снять с продажи третью игру. Её рынки
аннулируются каноническим `void_market`, поэтому ноги возвращаются по всем
купонам и остаются возвращёнными, а в экспрессе такая нога даёт 1.00.

Ставка на серию рассчитывается на строке-заголовке с «о счётом = победы в серии»
и не требует ни одного нового правила в `market_settler`.
"""

import os
import tempfile
import unittest

import database
from services import betting_engine
from services.market_settler import evaluate_market_selection

STAGE = "1/64"


class CupProgressionTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO seasons (name, status) VALUES ('Cup Progression Season', 'active')")
            self.season = cursor.lastrowid
            for tid, team in ((1, "Клуб А"), (2, "Клуб Б"), (3, "Клуб В"), (4, "Клуб Г")):
                cursor.execute(
                    "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                    "VALUES (?, ?, ?, 'player', datetime('now', '+3 hours'))",
                    (tid, f"coach_{tid}", team)
                )
            cursor.execute(
                "INSERT INTO users (telegram_id, username, team_name, role, registered_at) "
                "VALUES (9, 'bettor', 'Клуб Х', 'player', datetime('now', '+3 hours'))"
            )
        database.add_coins(9, 50_000)
        database.create_cup_series(
            STAGE, [("Клуб А", "Клуб Б"), ("Клуб В", "Клуб Г")], season_id=self.season
        )
        self.stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        database.provision_cup_stage_line(STAGE, season_id=self.season)
        ok, message = database.open_cup_stage_bets(self.stage_id)
        self.assertTrue(ok, message)
        betting_engine.generate_stage_markets(STAGE, season_id=self.season)

        rows = database.get_cup_stage_matches(STAGE, season_id=self.season)
        self.game = {(r["series_num"], r["game_num_in_series"]): r["match_id"]
                     for r in rows if not r["is_series_header"]}
        self.header = {r["series_num"]: r["match_id"] for r in rows if r["is_series_header"]}

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self._tmp.name + suffix)
            except OSError:
                pass

    # --- helpers -----------------------------------------------------------

    def _confirm(self, match_id, h, a, winner=None, reporter=1):
        if winner:
            ok, message = database.set_cup_game_winner(match_id, winner, actor_id=reporter)
            self.assertTrue(ok, message)
        return database.confirm_and_finalize_match(match_id, h, a, [], reporter_id=reporter)

    def _row(self, sql, params=()):
        with database.transaction() as conn:
            row = conn.cursor().execute(sql, params).fetchone()
        return dict(row) if row else None

    def _rows(self, sql, params=()):
        with database.transaction() as conn:
            return [dict(r) for r in conn.cursor().execute(sql, params).fetchall()]

    def _series(self, series_id=1):
        return self._row("SELECT * FROM cup_series WHERE id = ?", (series_id,))

    def _item(self, bet_id):
        return self._row("SELECT * FROM bet_items WHERE bet_id = ?", (bet_id,))

    def _balance(self):
        return database.get_or_create_wallet(9)["balance"]

    # --- 1. победитель при равном счёте ------------------------------------

    def test_01_equal_score_without_winner_does_not_confirm(self):
        before = self._row("SELECT status, player1_score FROM matches WHERE id = ?", (self.game[(1, 1)],))
        with self.assertRaises(ValueError):
            database.confirm_and_finalize_match(self.game[(1, 1)], 2, 2, [], reporter_id=1)
        after = self._row("SELECT status, player1_score FROM matches WHERE id = ?", (self.game[(1, 1)],))
        self.assertEqual(after["status"], "pending")
        self.assertEqual(after["player1_score"], before["player1_score"])
        self.assertIsNone(after["player1_score"], "откат обязан вернуть матч в исходное состояние")
        self.assertEqual(self._series()["team1_wins"], 0)

    def test_02_declared_winner_settles_1x2_and_keeps_totals_on_main_time(self):
        bet = database.place_user_bet(9, 100, [{"match_id": self.game[(1, 1)], "outcome": "p1"}])[1]
        self._confirm(self.game[(1, 1)], 2, 2, winner="Клуб А")

        series = self._series()
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (1, 0))
        self.assertEqual(
            self._row("SELECT cup_winner_team FROM matches WHERE id = ?", (self.game[(1, 1)],))["cup_winner_team"],
            "Клуб А"
        )
        item = self._item(bet)
        self.assertEqual(item["status"], "won", "П1 в кубке — победа в игре с учётом послематчевых")
        # Тот же матч: 2:2 — это четыре гола основного времени. Тоталы и ОЗ
        # считаются по нему, а не по «победителю с послематчевыми».
        self.assertEqual(evaluate_market_selection("total_goals", "over_2.5", 2, 2, "finished"), "won")
        self.assertEqual(evaluate_market_selection("btts", "btts_yes", 2, 2, "finished"), "won")

    def test_03_winner_side_wins_over_goal_comparison(self):
        """Победитель из поля, а не из счёта: 2:2 с прошедшим «Клуб Б» — это П2."""
        bet = database.place_user_bet(9, 100, [{"match_id": self.game[(1, 1)], "outcome": "p2"}])[1]
        self._confirm(self.game[(1, 1)], 2, 2, winner="Клуб Б")
        self.assertEqual(self._item(bet)["status"], "won")
        self.assertEqual(self._series()["team2_wins"], 1)

    def test_04_winner_must_belong_to_the_pair(self):
        ok, message = database.set_cup_game_winner(self.game[(1, 1)], "Клуб В")
        self.assertFalse(ok)
        self.assertIn("не играет", message)
        confirmed = self.game[(1, 1)]
        self._confirm(confirmed, 3, 0)
        ok, message = database.set_cup_game_winner(confirmed, "Клуб А")
        self.assertFalse(ok)
        self.assertIn("уже подтверждён", message)

    # --- 2. серия 2:0: третья игра снимается -------------------------------

    def test_05_sweep_voids_the_unplayed_game(self):
        third_bet = database.place_user_bet(
            9, 100, [{"match_id": self.game[(1, 3)], "outcome": "p1"}])[1]
        header_bet = database.place_user_bet(
            9, 100, [{"match_id": self.header[1], "outcome": "cs_2_0"}])[1]
        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 0)

        series = self._series()
        self.assertEqual(series["status"], "completed")
        self.assertEqual(series["winner_name"], "Клуб А")
        self.assertEqual(series["winner_source"], "main_time")

        third = self._row("SELECT status FROM matches WHERE id = ?", (self.game[(1, 3)],))
        self.assertEqual(third["status"], "cancelled")
        markets = self._rows(
            "SELECT status FROM markets WHERE match_id = ?", (self.game[(1, 3)],))
        self.assertTrue(markets)
        self.assertEqual({m["status"] for m in markets}, {"voided"})
        self.assertEqual(self._item(third_bet)["status"], "refunded")
        self.assertEqual(
            self._row("SELECT status, actual_payout FROM user_bets WHERE id = ?", (third_bet,))["status"],
            "refunded"
        )
        header = self._row("SELECT status, player1_score, player2_score FROM matches WHERE id = ?",
                           (self.header[1],))
        self.assertEqual(header["status"], "confirmed")
        self.assertEqual((header["player1_score"], header["player2_score"]), (2, 0))
        self.assertEqual(self._item(header_bet)["status"], "won")
        # Возврат третьей игры + выигрыш серии: деньги проведены один раз каждая.
        entries = self._rows(
            "SELECT transaction_type FROM coin_transactions WHERE user_id = 9 "
            "AND transaction_type IN ('bet_won','admin_refund') ORDER BY id")
        self.assertEqual([e["transaction_type"] for e in entries], ["admin_refund", "bet_won"])

    def test_06_voided_leg_pays_express_as_one(self):
        """Нога аннулированной игры в экспрессе = 1.00, купон живёт на второй ноге."""
        coupon = database.place_user_bet(9, 100, [
            {"match_id": self.game[(1, 3)], "outcome": "p1"},
            {"match_id": self.game[(2, 1)], "outcome": "p1"},
        ])[1]
        odd_second = self._row("SELECT odd FROM bet_items WHERE bet_id = ? AND match_id = ?",
                               (coupon, self.game[(2, 1)]))["odd"]
        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 0)
        self._confirm(self.game[(2, 1)], 3, 2)

        bet = self._row("SELECT status, actual_payout FROM user_bets WHERE id = ?", (coupon,))
        self.assertEqual(bet["status"], "won")
        self.assertEqual(bet["actual_payout"], int(round(100 * float(odd_second))))
        legs = self._rows("SELECT match_id, status FROM bet_items WHERE bet_id = ? ORDER BY id", (coupon,))
        self.assertEqual([l["status"] for l in legs], ["refunded", "won"])

    def test_07_decided_series_is_not_settled_twice(self):
        header_bet = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "p1"}])[1]
        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 0)
        payout_first = self._row("SELECT actual_payout FROM user_bets WHERE id = ?", (header_bet,))["actual_payout"]
        balance = self._balance()

        # Повторное подтверждение той же игры не должно считать серию заново.
        self._confirm(self.game[(1, 2)], 1, 0)
        self.assertEqual(self._balance(), balance)
        self.assertEqual(
            self._row("SELECT actual_payout FROM user_bets WHERE id = ?", (header_bet,))["actual_payout"],
            payout_first
        )

    # --- 3. 2:1 и обратный ход ---------------------------------------------

    def test_08_deciding_game_on_penalties_marks_the_series_source(self):
        third_bet = database.place_user_bet(9, 100, [{"match_id": self.game[(1, 3)], "outcome": "p2"}])[1]
        score_bet = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "cs_1_2"}])[1]
        third_game = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "over_2.5"}])[1]

        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 1, winner="Клуб Б")
        self._confirm(self.game[(1, 3)], 3, 3, winner="Клуб Б")

        series = self._series()
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (1, 2))
        self.assertEqual(series["winner_name"], "Клуб Б")
        self.assertEqual(series["winner_source"], "penalties",
                         "серию решила игра, у которой равное основное время")
        self.assertEqual(self._item(third_bet)["status"], "won")
        self.assertEqual(self._item(score_bet)["status"], "won")
        self.assertEqual(self._item(third_game)["status"], "won")
        header = self._row("SELECT player1_score, player2_score FROM matches WHERE id = ?", (self.header[1],))
        self.assertEqual((header["player1_score"], header["player2_score"]), (1, 2))
        self.assertEqual(self._row("SELECT status FROM matches WHERE id = ?", (self.game[(1, 3)],))["status"],
                         "confirmed")

    def test_09_reset_keeps_a_penalties_win_credited(self):
        """Сброс одной игры не должен терять победу, засчитанную по послематчевым.

        Старый пересчёт выводил победы из сравнения голов: на 2:2 он не давал
        ничего, и после сброса соседней игры серия «забывала» сыгранную игру.
        """
        self._confirm(self.game[(1, 1)], 2, 2, winner="Клуб А")
        self._confirm(self.game[(1, 2)], 1, 0)
        self.assertEqual(self._series()["status"], "completed")

        database.reset_match(self.game[(1, 2)])
        series = self._series()
        self.assertEqual(series["status"], "active")
        self.assertIsNone(series["winner_name"])
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (1, 0),
                         "победа 2:2 по послематчевым осталась засчитанной")

    # --- 4. серия, закончившаяся после старта этапа --------------------------

    def test_11_sweep_after_stage_start_refunds_the_third_game(self):
        """Старт этапа закрывает линию (`closed`); 2:0 обязано вернуть и закрытые рынки.

        Обычный порядок жизни: ставки → старт этапа → игры. Снятие третьей игры
        брало только open/suspended, и ставки на неё висели pending навсегда.
        """
        third_bet = database.place_user_bet(
            9, 100, [{"match_id": self.game[(1, 3)], "outcome": "p1"}])[1]
        header_bet = database.place_user_bet(
            9, 100, [{"match_id": self.header[1], "outcome": "cs_2_0"}])[1]
        ok, message = database.start_cup_stage(self.stage_id, actor_id=1)
        self.assertTrue(ok, message)
        self.assertEqual(
            {m["status"] for m in self._rows("SELECT status FROM markets WHERE match_id = ?",
                                              (self.game[(1, 3)],))},
            {"closed"}
        )

        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 0)

        self.assertEqual(
            {m["status"] for m in self._rows("SELECT status FROM markets WHERE match_id = ?",
                                              (self.game[(1, 3)],))},
            {"voided"}
        )
        self.assertEqual(self._item(third_bet)["status"], "refunded")
        self.assertEqual(
            self._row("SELECT status FROM user_bets WHERE id = ?", (third_bet,))["status"], "refunded")
        self.assertEqual(self._item(header_bet)["status"], "won")

    # --- 5. сброс игры в решённой серии -------------------------------------

    def test_12_reset_keeping_the_series_decided_resettles_the_header(self):
        """2:1 → сброс проигранной игры → 2:0: серия решена, счёт заголовка — новый."""
        score_21 = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "cs_2_1"}])[1]
        score_20 = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "cs_2_0"}])[1]
        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 0, 1)
        self._confirm(self.game[(1, 3)], 3, 1)
        self.assertEqual(self._item(score_21)["status"], "won")
        self.assertEqual(self._item(score_20)["status"], "lost")

        database.reset_match(self.game[(1, 2)])

        series = self._series()
        self.assertEqual(series["status"], "completed")
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (2, 0))
        self.assertEqual(series["winner_name"], "Клуб А")
        header = self._row("SELECT player1_score, player2_score FROM matches WHERE id = ?", (self.header[1],))
        self.assertEqual((header["player1_score"], header["player2_score"]), (2, 0))
        self.assertEqual(self._item(score_21)["status"], "lost")
        self.assertEqual(self._item(score_20)["status"], "won")

    def test_13_reset_undeciding_the_series_reopens_the_third_game(self):
        """2:0 → сброс игры 2 → 1:0: серия активна, снятая игра 3 снова в сетке.

        Когда серия решится заново другим клубом, ставки на заголовок обязаны
        перерасчитаться, а не остаться с выплатой прежнего прохода.
        """
        through_a = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "p1"}])[1]
        through_b = database.place_user_bet(9, 100, [{"match_id": self.header[1], "outcome": "p2"}])[1]
        self._confirm(self.game[(1, 1)], 2, 0)
        self._confirm(self.game[(1, 2)], 1, 0)
        self.assertEqual(
            self._row("SELECT status FROM matches WHERE id = ?", (self.game[(1, 3)],))["status"], "cancelled")
        self.assertEqual(self._item(through_a)["status"], "won")

        database.reset_match(self.game[(1, 2)])

        series = self._series()
        self.assertEqual(series["status"], "active")
        self.assertIsNone(series["winner_name"])
        self.assertIsNone(series["winner_source"])
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (1, 0))
        third = self._row("SELECT status, player1_score, cup_winner_team FROM matches WHERE id = ?",
                          (self.game[(1, 3)],))
        self.assertEqual(third["status"], "pending")
        self.assertIsNone(third["player1_score"])
        self.assertEqual(
            {m["status"] for m in self._rows("SELECT status FROM markets WHERE match_id = ?",
                                              (self.game[(1, 3)],))},
            {"voided"},
            "void финален: игра возвращается в сетку без линии"
        )

        self._confirm(self.game[(1, 2)], 0, 1)
        self._confirm(self.game[(1, 3)], 1, 2)

        series = self._series()
        self.assertEqual(series["status"], "completed")
        self.assertEqual(series["winner_name"], "Клуб Б")
        header = self._row("SELECT player1_score, player2_score FROM matches WHERE id = ?", (self.header[1],))
        self.assertEqual((header["player1_score"], header["player2_score"]), (1, 2))
        self.assertEqual(self._item(through_a)["status"], "lost")
        self.assertEqual(self._item(through_b)["status"], "won")
        self.assertEqual(
            self._row("SELECT actual_payout FROM user_bets WHERE id = ?", (through_a,))["actual_payout"] or 0, 0,
            "выплата прежнего прохода отозвана"
        )

    # --- 6. победитель не спорит со счётом ----------------------------------

    def test_14_decisive_score_overrides_a_stale_winner(self):
        """Ответ о послематчевых, данный до счёта 2:0 за другой клуб, не решает игру."""
        p1 = database.place_user_bet(9, 100, [{"match_id": self.game[(1, 1)], "outcome": "p1"}])[1]
        ok, message = database.set_cup_game_winner(self.game[(1, 1)], "Клуб Б", actor_id=1)
        self.assertTrue(ok, message)

        self._confirm(self.game[(1, 1)], 2, 0)

        series = self._series()
        self.assertEqual((series["team1_wins"], series["team2_wins"]), (1, 0))
        self.assertEqual(
            self._row("SELECT cup_winner_team FROM matches WHERE id = ?", (self.game[(1, 1)],))["cup_winner_team"],
            "Клуб А"
        )
        self.assertEqual(self._item(p1)["status"], "won")

    def test_15_winner_contradicting_a_decisive_score_is_rejected(self):
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET player1_score = 3, player2_score = 1 WHERE id = ?", (self.game[(1, 1)],))
        ok, message = database.set_cup_game_winner(self.game[(1, 1)], "Клуб Б", actor_id=1)
        self.assertFalse(ok)
        self.assertIn("3:1", message)
        ok, message = database.set_cup_game_winner(self.game[(1, 1)], "Клуб А", actor_id=1)
        self.assertTrue(ok, message)

    def test_16_reset_clears_the_declared_winner(self):
        self._confirm(self.game[(1, 1)], 2, 2, winner="Клуб А")
        database.reset_match(self.game[(1, 1)])
        self.assertIsNone(
            self._row("SELECT cup_winner_team FROM matches WHERE id = ?", (self.game[(1, 1)],))["cup_winner_team"])
        with self.assertRaises(ValueError, msg="переигровка вничью требует нового ответа"):
            database.confirm_and_finalize_match(self.game[(1, 1)], 1, 1, [], reporter_id=1)

    def test_10_cup_games_never_open_debts(self):
        """Дисциплина кубка — напоминания, а не долги: `match_debts` пустой."""
        database.sync_match_debts()
        cup_ids = {self.game[(1, 1)], self.game[(1, 2)], self.game[(1, 3)], self.header[1]}
        rows = self._rows("SELECT match_id FROM match_debts")
        self.assertEqual([r["match_id"] for r in rows if r["match_id"] in cup_ids], [])
        self._confirm(self.game[(1, 1)], 2, 0)
        database.sync_match_debts()
        rows = self._rows("SELECT match_id FROM match_debts")
        self.assertEqual([r["match_id"] for r in rows if r["match_id"] in cup_ids], [])


class CupWinnerPromptTest(unittest.TestCase):
    """Шаг приёмки результата в кабинете: когда он обязателен.

    Сама кнопка проверяется отдельно от транзакции: ошибка здесь двусторонняя —
    спросить ответ там, где его не за чем спрашивать (лига, явный победитель), или
    не спросить на 2:2 и получить отказ от `advance_cup_series`.
    """

    def _match(self, **over):
        base = {"id": 1, "tournament_type": "cup", "cup_winner_team": None,
                "player1_team": "Клуб А", "player2_team": "Клуб Б"}
        base.update(over)
        return base

    def test_prompt_only_for_a_cup_game_without_a_declared_winner(self):
        from handlers.cabinet import _cup_winner_needed
        self.assertTrue(_cup_winner_needed(self._match(), 2, 2))
        self.assertFalse(_cup_winner_needed(self._match(), 2, 1))
        self.assertFalse(_cup_winner_needed(self._match(cup_winner_team="Клуб А"), 2, 2))
        self.assertFalse(_cup_winner_needed(self._match(tournament_type="league"), 2, 2))
        self.assertFalse(_cup_winner_needed(self._match(tournament_type=None), 2, 2),
                         "строка без типа матча — лиговая, кубковых вопросов не задаём")

    def test_winner_keyboard_offers_both_clubs_and_the_same_confirm_button(self):
        from handlers.cabinet import _cup_winner_keyboard
        markup = _cup_winner_keyboard(7, "Клуб А", "Клуб Б", "cb_confirm_ai_final_7")
        data = [[b.callback_data for b in row] for row in markup.inline_keyboard]
        self.assertEqual(data, [["cb_cup_winner_7_1", "cb_cup_winner_7_2"], ["cb_confirm_ai_final_7"]],
                         "выбор победителя — шаг перед тем же подтверждением, а не вместо него")


if __name__ == "__main__":
    unittest.main()
