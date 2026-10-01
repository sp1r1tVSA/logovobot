"""Матчи исключённого игрока ждут замену.

Авто-исключение по варнам (`ban_and_remove_from_league`) замораживает несыгранные
матчи клуба (`vacancy_frozen = 1`), пока у клуба нет тренера: долгом они не
становятся, напоминания и варны никому не уходят. Новый тренер
(`set_player_club`) размораживает их — дедлайн и сроки долга сдвигаются на время
вакансии (`debt_policy.match_clock`). Ставки на эти матчи всё время висят `pending`.
"""

import datetime
import os
import tempfile
import unittest

import config
import database
from services import debt_policy
from time_utils import now_msk


def _fmt(dt: datetime.datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M")


def _ts(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


class TestMatchClock(unittest.TestCase):
    """Чистая политика: заморозка сдвигает и дедлайн тура."""

    NOW = datetime.datetime(2026, 10, 1, 12, 0)

    def _round(self, deadline):
        return {"is_open": 1, "status": "open", "deadline": _fmt(deadline)}

    def test_clock_subtracts_banked_and_running_freeze(self):
        match = {"is_extended": 1, "frozen_at": _ts(self.NOW - datetime.timedelta(hours=2)),
                 "frozen_seconds": 3600}
        self.assertEqual(debt_policy.match_clock(match, self.NOW), self.NOW - datetime.timedelta(hours=3))

    def test_unfrozen_match_keeps_wall_clock(self):
        self.assertEqual(debt_policy.match_clock({}, self.NOW), self.NOW)

    def test_freeze_before_deadline_postpones_the_debt(self):
        # Дедлайн прошёл час назад, но матч стоял 5 часов — до долга ещё 4 часа.
        rnd = self._round(self.NOW - datetime.timedelta(hours=1))
        match = {"status": "pending", "frozen_seconds": 5 * 3600}
        self.assertFalse(debt_policy.is_debt(match, rnd, None, self.NOW))
        self.assertTrue(debt_policy.is_debt(match, rnd, None, self.NOW + datetime.timedelta(hours=4)))

    def test_running_vacancy_never_becomes_a_debt(self):
        rnd = self._round(self.NOW - datetime.timedelta(days=3))
        match = {"status": "pending", "is_extended": 1,
                 "frozen_at": _ts(self.NOW - datetime.timedelta(days=4))}
        self.assertFalse(debt_policy.is_debt(match, rnd, None, self.NOW))

    def test_freeze_after_deadline_keeps_the_debt(self):
        rnd = self._round(self.NOW - datetime.timedelta(hours=10))
        match = {"status": "pending", "frozen_seconds": 2 * 3600}
        self.assertTrue(debt_policy.is_debt(match, rnd, None, self.NOW))

    def test_match_played_inside_the_shifted_window_is_not_a_debt(self):
        rnd = self._round(self.NOW - datetime.timedelta(hours=10))
        match = {"status": "confirmed", "frozen_seconds": 24 * 3600,
                 "played_at": _ts(self.NOW - datetime.timedelta(hours=1))}
        self.assertFalse(debt_policy.is_debt(match, rnd, None, self.NOW + datetime.timedelta(days=5)))


class TestVacantClubFreeze(unittest.TestCase):
    """Отдельная временная БД на каждый тест."""

    CLUB = "Vacant Club FC"
    RIVAL = "Rival Club FC"
    OTHER = "Other Club FC"
    THIRD = "Third Club FC"

    KICKED_ID = 8811001
    RIVAL_ID = 8811002
    OTHER_ID = 8811003
    THIRD_ID = 8811004
    NEW_COACH_ID = 8811005
    BETTOR_ID = 8811006

    ROUND = 4
    PENDING_ID = 7801       # клуб дома
    AWAY_ID = 7802          # клуб в гостях
    PLAYED_ID = 7803        # уже сыгран
    FOREIGN_ID = 7804       # чужой матч
    STAKE = 100

    def setUp(self):
        tf = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db_path = tf.name
        tf.close()
        self.orig_config_path = config.DB_PATH
        self.orig_database_path = database.DB_PATH
        config.DB_PATH = self.temp_db_path
        database.DB_PATH = self.temp_db_path
        database.init_db()

        self.division_id = database.create_division(name="Vacancy Div", code="VACDIV")
        season = database.get_active_season()
        self.season_id = season["id"] if season else 1

        for tg_id, name, club in (
            (self.KICKED_ID, "vac_kicked", self.CLUB),
            (self.RIVAL_ID, "vac_rival", self.RIVAL),
            (self.OTHER_ID, "vac_other", self.OTHER),
            (self.THIRD_ID, "vac_third", self.THIRD),
            (self.NEW_COACH_ID, "vac_new", None),
            (self.BETTOR_ID, "vac_bettor", None),
        ):
            database.register_user(tg_id, name, team_name=club)
            database.assign_user_division(tg_id, self.division_id)

        self.deadline = now_msk() + datetime.timedelta(hours=10)
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO rounds (round_number, division_id, season_id, is_open, bets_open, deadline) "
                "VALUES (?, ?, ?, 1, 1, ?)",
                (self.ROUND, self.division_id, self.season_id, _fmt(self.deadline)),
            )
            for mid, p1, p2, t1, t2, status, s1, s2 in (
                (self.PENDING_ID, self.KICKED_ID, self.RIVAL_ID, self.CLUB, self.RIVAL, "pending", None, None),
                (self.AWAY_ID, self.OTHER_ID, self.KICKED_ID, self.OTHER, self.CLUB, "pending", None, None),
                (self.PLAYED_ID, self.KICKED_ID, self.THIRD_ID, self.CLUB, self.THIRD, "confirmed", 2, 1),
                (self.FOREIGN_ID, self.RIVAL_ID, self.THIRD_ID, self.RIVAL, self.THIRD, "pending", None, None),
            ):
                c.execute(
                    "INSERT INTO matches (id, tournament_id, round_number, player1_id, player2_id, "
                    "player1_team, player2_team, player1_score, player2_score, status, division_id, "
                    "season_id, tournament_type) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'league')",
                    (mid, self.ROUND, p1, p2, t1, t2, s1, s2, status, self.division_id, self.season_id),
                )

    def tearDown(self):
        config.DB_PATH = self.orig_config_path
        database.DB_PATH = self.orig_database_path
        try:
            os.remove(self.temp_db_path)
        except Exception:
            pass

    # ---------------------------------------------------------------- helpers

    def _match(self, match_id: int) -> dict:
        with database.transaction() as conn:
            return dict(conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)).fetchone())

    def _set_deadline(self, deadline: datetime.datetime):
        with database.transaction() as conn:
            conn.execute(
                "UPDATE rounds SET deadline = ? WHERE round_number = ? AND division_id = ?",
                (_fmt(deadline), self.ROUND, self.division_id),
            )

    def _backdate_freeze(self, hours: float):
        """Вакансия началась `hours` часов назад."""
        when = _ts(now_msk() - datetime.timedelta(hours=hours))
        with database.transaction() as conn:
            conn.execute("UPDATE matches SET frozen_at = ? WHERE vacancy_frozen = 1", (when,))

    def _kick(self):
        self.assertEqual(database.ban_and_remove_from_league(self.KICKED_ID), self.CLUB)

    # ------------------------------------------------------------------ tests

    def test_kick_freezes_only_the_clubs_unplayed_matches(self):
        with database.transaction() as conn:
            conn.execute(
                "UPDATE matches SET is_extended = 1, frozen_at = ?, extended_until = ? WHERE id = ?",
                (_ts(now_msk()), _ts(now_msk() + datetime.timedelta(hours=24)), self.PENDING_ID),
            )
        self._kick()

        home, away = self._match(self.PENDING_ID), self._match(self.AWAY_ID)
        for m in (home, away):
            self.assertEqual((m["is_extended"], m["vacancy_frozen"]), (1, 1))
            self.assertIsNotNone(m["frozen_at"])
            self.assertIsNone(m["extended_until"])
        self.assertIsNone(home["player1_id"])
        self.assertEqual(home["player2_id"], self.RIVAL_ID)
        self.assertEqual(away["player1_id"], self.OTHER_ID)
        self.assertIsNone(away["player2_id"])

        for mid in (self.PLAYED_ID, self.FOREIGN_ID):
            m = self._match(mid)
            self.assertEqual((m["is_extended"] or 0, m["vacancy_frozen"]), (0, 0))
        self.assertEqual(self._match(self.PLAYED_ID)["player1_id"], self.KICKED_ID)

    def test_vacancy_produces_no_debt_after_the_deadline(self):
        self._kick()
        self._backdate_freeze(30)
        self._set_deadline(now_msk() - datetime.timedelta(hours=20))

        database.sync_match_debts()

        self.assertIsNone(database.get_match_debt(self.PENDING_ID))
        self.assertFalse(database.is_match_overdue(self.PENDING_ID))
        # Чужой матч того же тура долгом стал — правило не отключено целиком.
        self.assertIsNotNone(database.get_match_debt(self.FOREIGN_ID))

    def test_new_coach_unfreezes_and_inherits_the_remaining_time(self):
        self._kick()
        # Клуб ждал 30 ч; дедлайн (10 ч после исключения) прошёл 20 ч назад.
        self._backdate_freeze(30)
        self._set_deadline(now_msk() - datetime.timedelta(hours=20))

        ok, message = database.set_player_club(str(self.NEW_COACH_ID), self.CLUB)
        self.assertTrue(ok)
        self.assertIn("Разморожено матчей клуба: 2", message)

        home, away = self._match(self.PENDING_ID), self._match(self.AWAY_ID)
        for m in (home, away):
            self.assertEqual((m["is_extended"], m["vacancy_frozen"]), (0, 0))
            self.assertGreaterEqual(m["frozen_seconds"], 30 * 3600 - 60)
        self.assertEqual(home["player1_id"], self.NEW_COACH_ID)
        self.assertEqual(away["player2_id"], self.NEW_COACH_ID)
        self.assertEqual(database.get_match(self.PENDING_ID)["player1_id"], self.NEW_COACH_ID)

        # У нового тренера те же ~10 ч, что оставались в момент исключения.
        database.sync_match_debts()
        self.assertIsNone(database.get_match_debt(self.PENDING_ID))
        self.assertFalse(database.is_match_overdue(self.PENDING_ID))
        database.sync_match_debts(now=now_msk() + datetime.timedelta(hours=11))
        self.assertIsNotNone(database.get_match_debt(self.PENDING_ID))

    def test_admin_freeze_is_left_alone(self):
        with database.transaction() as conn:
            conn.execute(
                "UPDATE matches SET player1_team = ?, player1_id = ? WHERE id = ?",
                (self.CLUB, self.RIVAL_ID, self.FOREIGN_ID),
            )
        self._kick()
        # Матч заморожен уже после исключения — его заморозил админ, не вакансия.
        with database.transaction() as conn:
            conn.execute("UPDATE matches SET vacancy_frozen = 0 WHERE id = ?", (self.FOREIGN_ID,))

        ok, message = database.set_player_club(str(self.NEW_COACH_ID), self.CLUB)
        self.assertTrue(ok)
        self.assertIn("Разморожено матчей клуба: 2", message)
        self.assertEqual(self._match(self.FOREIGN_ID)["is_extended"], 1)

    def test_bets_wait_for_the_result(self):
        # Ставки принимаются, пока тур не открыт.
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET is_open = 0, status = 'scheduled' WHERE division_id = ?",
                         (self.division_id,))
        database.save_bet_market(
            self.PENDING_ID, self.ROUND, self.CLUB, self.RIVAL,
            2.00, 3.30, 3.10, 1.80, 1.90, 1.75, 2.00,
        )
        database.get_or_create_wallet(self.BETTOR_ID)
        start = database.get_wallet_balance(self.BETTOR_ID)
        ok, bet_id = database.place_user_bet(
            self.BETTOR_ID, self.STAKE, [{"match_id": self.PENDING_ID, "outcome": "p1", "odd": 2.00}]
        )
        self.assertTrue(ok, bet_id)

        self._kick()
        self.assertEqual([b["status"] for b in database.get_user_bets(self.BETTOR_ID)], ["pending"])
        database.set_player_club(str(self.NEW_COACH_ID), self.CLUB)

        self.assertEqual([b["status"] for b in database.get_user_bets(self.BETTOR_ID)], ["pending"])
        self.assertEqual(database.get_wallet_balance(self.BETTOR_ID), start - self.STAKE)


if __name__ == "__main__":
    unittest.main()
