"""
tests/test_cup_betting_gate.py

Единый гейт приёма ставок: лига и кубок проверяются одним предикатом
(`database._evaluate_gate_row`), различается только строка-разрешение.

 1. Этап кубка не принимает ставок, пока линия не открыта, и принимает после.
 2. Открыть линию нечего — отказ; открыть дважды — отказ; начать этап — линия
    закрывается тем же переходом (инвариант `is_open = 1 ⇒ bets_open = 0`).
 3. Дедлайн этапа факультативен: без него линию закрывает только старт этапа.
 4. Сыгранный матч не принимается и при открытой линии.
 5. Диспетчер не путает скоупы: кубковая строка не разрешает ставку на лиговый
    матч и наоборот, а кубковый матч без привязки к этапу получает отказ.
 6. Тот же путь проходит RiskEngine со своим узким SELECT-ом (без `stage_id`
    в списке колонок кубок молча уехал бы в лиговую ветку).
"""

import datetime as dt
import os
import tempfile
import unittest

import database
from time_utils import now_msk


STAGE = "1/64"
OTHER_STAGE = "1/32"


def _msk_text(offset_minutes: int) -> str:
    return (now_msk() + dt.timedelta(minutes=offset_minutes)).strftime("%Y-%m-%d %H:%M:%S")


class CupGateTestCase(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()
        database.ensure_canonical_divisions()
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("INSERT INTO seasons (name, status) VALUES ('Cup Gate Season', 'active')")
            self.season = c.lastrowid

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    # --- helpers ---------------------------------------------------------

    def _seed_stage(self, stage=STAGE, matches=1, deadline=None):
        """Этап + сетка + нужное число игр; возвращает (stage_id, [match_id])."""
        if deadline is not None:
            database.create_cup_stage(stage, season_id=self.season, deadline=deadline)
        pairs = [(f"Cup Club {i * 2 + 1}", f"Cup Club {i * 2 + 2}") for i in range(matches)]
        database.create_cup_series(stage, pairs, season_id=self.season)
        stage_row = database.get_cup_stage(stage, season_id=self.season)
        ids = []
        for series in database.get_cup_bracket(stage, season_id=self.season):
            ids.append(database.create_cup_series_match(series["id"]))
        return stage_row["id"], ids

    def _gate(self, match_id, stage_id):
        with database.transaction() as conn:
            return database.evaluate_cup_stage_gate(conn.cursor(), stage_id, match_id=match_id)

    def _dispatch(self, match_id):
        with database.transaction() as conn:
            row = conn.cursor().execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone()
            return database.evaluate_betting_gate(conn.cursor(), row, match_id=match_id)

    def _confirm(self, match_id, s1=2, s2=1, winner=None):
        with database.transaction() as conn:
            conn.cursor().execute(
                "UPDATE matches SET status = 'confirmed', player1_score = ?, player2_score = ?, "
                "cup_winner_team = ? WHERE id = ?",
                (s1, s2, winner, match_id)
            )

    # --- 1-2. состояние линии -------------------------------------------

    def test_01_closed_line_refuses_and_names_the_stage(self):
        stage_id, [match_id] = self._seed_stage()
        allowed, reason, message = self._gate(match_id, stage_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "LINE_CLOSED")
        self.assertIn(STAGE, message)

    def test_02_open_line_with_no_matches_is_refused(self):
        database.create_cup_stage(STAGE, season_id=self.season)
        stage_id = database.get_cup_stage(STAGE, season_id=self.season)["id"]
        self.assertEqual(database.count_cup_stage_matches(stage_id), 0)
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertFalse(ok)
        self.assertIn("нет матчей", message)

    def test_03_opened_line_admits_a_cup_match(self):
        stage_id, [match_id] = self._seed_stage()
        ok, _message = database.open_cup_stage_bets(stage_id)
        self.assertTrue(ok)
        allowed, reason, message = self._gate(match_id, stage_id)
        self.assertTrue(allowed, f"{reason}: {message}")
        self.assertEqual(database.get_cup_stage(STAGE, season_id=self.season)["bets_open"], 1)
        # Тот же ответ должен давать и диспетчер, через который идёт `place_user_bet`.
        self.assertTrue(self._dispatch(match_id)[0])

    def test_04_second_open_is_refused(self):
        stage_id, _matches = self._seed_stage()
        self.assertTrue(database.open_cup_stage_bets(stage_id)[0])
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertFalse(ok)
        self.assertIn("уже открыт", message)

    def test_05_starting_the_stage_closes_the_line(self):
        """Ровно тот переход, которым владелец закрывает приём прогнозов."""
        stage_id, [match_id] = self._seed_stage()
        database.open_cup_stage_bets(stage_id)
        ok, _message = database.start_cup_stage(stage_id, actor_id=None)
        self.assertTrue(ok)

        stage = database.get_cup_stage(STAGE, season_id=self.season)
        self.assertEqual(stage["is_open"], 1)
        self.assertEqual(stage["bets_open"], 0, "is_open = 1 AND bets_open = 1 недопустимо")

        allowed, reason, _message = self._gate(match_id, stage_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "ROUND_STARTED")
        self.assertEqual(self._dispatch(match_id)[:2], (False, "ROUND_STARTED"))

    def test_06_line_cannot_be_returned_to_a_started_stage(self):
        stage_id, _matches = self._seed_stage()
        database.start_cup_stage(stage_id)
        ok, message = database.open_cup_stage_bets(stage_id)
        self.assertFalse(ok)
        self.assertIn("уже открыт для игры", message)

    # --- 3. дедлайн ------------------------------------------------------

    def test_07_passed_deadline_refuses(self):
        stage_id, [match_id] = self._seed_stage(deadline=_msk_text(-60))
        self.assertTrue(database.open_cup_stage_bets(stage_id)[0])

        allowed, reason, _message = self._gate(match_id, stage_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "DEADLINE_PASSED")

    def test_08_future_deadline_still_admits(self):
        stage_id, [match_id] = self._seed_stage(deadline=_msk_text(180))
        self.assertTrue(database.open_cup_stage_bets(stage_id)[0])
        self.assertTrue(self._gate(match_id, stage_id)[0])

    # --- 4. pre-match ----------------------------------------------------

    def test_09_finished_match_refuses_with_line_open(self):
        stage_id, [match_id] = self._seed_stage()
        database.open_cup_stage_bets(stage_id)
        self._confirm(match_id, winner="Cup Club 1")
        allowed, reason, _message = self._gate(match_id, stage_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "MATCH_FINISHED")

    # --- 5-6. диспетчер и скоупы ----------------------------------------

    def test_10_league_match_is_not_admitted_by_an_open_cup_stage(self):
        """Кубковая строка не может разрешить ставку на матч лиги.

        Номер тура лиги намеренно совпадает с порядком этапа: если бы скоуп
        искали только по числу, такая пара уже открытого этапа и не открытого
        тура давала бы разрешающий ответ.
        """
        stage_id, _cup_matches = self._seed_stage()
        database.open_cup_stage_bets(stage_id)

        round_number = database.CUP_STAGE_ORDER[STAGE]
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                "VALUES (?, 1, ?, 0, 0, NULL)",
                (round_number, self.season)
            )
            c.execute(
                "INSERT INTO matches (round_number, division_id, season_id, player1_team, player2_team, status) "
                "VALUES (?, 1, ?, 'League Home', 'League Away', 'pending')",
                (round_number, self.season)
            )
            league_match = c.lastrowid

        allowed, reason, _message = self._dispatch(league_match)
        self.assertFalse(allowed, "открытый этап кубка не должен разрешать ставку на матч лиги")
        self.assertEqual(reason, "LINE_CLOSED")

    def test_11_cup_match_without_stage_is_refused(self):
        """Привязки нет — разрешающего fallback нет."""
        _stage_id, [match_id] = self._seed_stage()
        with database.transaction() as conn:
            conn.cursor().execute("UPDATE matches SET stage_id = NULL WHERE id = ?", (match_id,))
        allowed, reason, _message = self._dispatch(match_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "ROUND_NOT_FOUND")

    def test_12_dispatcher_routes_the_narrow_row_risk_engine_selects(self):
        """Ровно тот набор колонок, который читает RiskEngine.

        Если в этом SELECT не окажется `stage_id`/`tournament_type`, кубковый матч
        уедет в лиговую ветку и получит отказ — при полностью открытой линии.
        """
        stage_id, [match_id] = self._seed_stage()
        database.open_cup_stage_bets(stage_id)
        with database.transaction() as conn:
            row = conn.cursor().execute(
                "SELECT status, division_id, season_id, round_number, tournament_type, stage_id "
                "FROM matches WHERE id = ?",
                (match_id,)
            ).fetchone()
            allowed, reason, message = database.evaluate_betting_gate(conn.cursor(), row, match_id=match_id)
        self.assertTrue(allowed, f"{reason}: {message}")
        self.assertEqual(row["stage_id"], stage_id)

    def test_13_other_stage_does_not_admit_this_match(self):
        """Открытый 1/32 не разрешает ставку на игру этапа 1/64."""
        stage_id, [match_id] = self._seed_stage()
        other_id, other_matches = self._seed_stage(stage=OTHER_STAGE)
        database.open_cup_stage_bets(other_id)

        allowed, reason, _message = self._gate(match_id, stage_id)
        self.assertFalse(allowed)
        self.assertEqual(reason, "LINE_CLOSED")
        # ...а на свою игру — разрешает: отказ выше именно по скоупу, не по правилам.
        self.assertTrue(self._gate(other_matches[0], other_id)[0])

    def test_14_unknown_stage_id_is_refused(self):
        _stage_id, [match_id] = self._seed_stage()
        allowed, reason, _message = self._gate(match_id, 999999)
        self.assertFalse(allowed)
        self.assertEqual(reason, "STAGE_NOT_FOUND")


class GateSharedPredicateTestCase(unittest.TestCase):
    """Лига и кубок обязаны отвечать одинаково на одинаковое состояние линии."""

    def test_15_messages_differ_only_by_scope_label(self):
        import inspect
        source = inspect.getsource(database._evaluate_gate_row)
        body = source.replace(database._evaluate_gate_row.__doc__ or "", "")
        # Ни «Тур», ни «Этап» в самом предикате не живут: текст собирается из scope_label.
        self.assertNotIn("Тур", body)
        self.assertNotIn("Этап", body)
        for reason in ("LINE_CLOSED", "DEADLINE_PASSED", "MATCH_FINISHED", "ROUND_STARTED"):
            self.assertIn(reason, body)


class CupLineLockTestCase(unittest.TestCase):
    """Переходы линии берут тот же процессный lock, что и приём ставок."""

    def setUp(self):
        self._orig_db_path = database.DB_PATH
        self._tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self._tmp.close()
        database.DB_PATH = self._tmp.name
        database.init_db()

    def tearDown(self):
        database.close_thread_connection()
        database.DB_PATH = self._orig_db_path
        try:
            os.remove(self._tmp.name)
        except OSError:
            pass

    def test_16_stage_and_round_line_share_the_placement_lock(self):
        import inspect
        for fn in (database.open_cup_stage_bets, database.start_cup_stage):
            source = inspect.getsource(fn)
            self.assertIn("_bet_placement_lock", source, f"{fn.__name__} меняет линию без сериализации")
