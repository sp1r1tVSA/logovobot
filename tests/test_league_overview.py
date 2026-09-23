"""`/overview` — сводка по всем дивизионам: сборка снимков, запрос к базе, права."""

import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
from handlers import league_overview as handler
from services import league_overview as ovw
from time_utils import now_msk

NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=now_msk().tzinfo)
MAX_WARNS = 4


def _dl(delta: datetime.timedelta) -> str:
    return (NOW + delta).strftime("%d.%m.%Y %H:%M")


def _rows(rounds=(), counts=(), warned=()):
    return {"season_id": 1, "rounds": list(rounds), "match_counts": list(counts), "warned_users": list(warned)}


class TestBuildSnapshots(unittest.TestCase):
    DIVS = [{"id": 1, "name": "Дивизион 1"}, {"id": 2, "name": "Дивизион 2"}]

    def test_open_and_overdue_rounds_are_active_with_progress(self):
        rows = _rows(
            rounds=[
                {"division_id": 1, "round_number": 1, "is_open": 0, "status": "closed", "deadline": _dl(-datetime.timedelta(days=5))},
                {"division_id": 1, "round_number": 2, "is_open": 1, "status": "open", "deadline": _dl(-datetime.timedelta(hours=3))},
                {"division_id": 1, "round_number": 3, "is_open": 1, "status": "open", "deadline": _dl(datetime.timedelta(days=2))},
                {"division_id": 1, "round_number": 4, "is_open": 0, "status": "scheduled", "deadline": None},
            ],
            counts=[
                {"division_id": 1, "round_number": 1, "total": 8, "played": 8},
                {"division_id": 1, "round_number": 2, "total": 8, "played": 6},
                {"division_id": 1, "round_number": 3, "total": 8, "played": 2},
                {"division_id": 1, "round_number": 4, "total": 8, "played": 0},
            ],
        )
        snap = ovw.build_snapshots(self.DIVS[:1], rows, [], NOW, MAX_WARNS)[0]

        self.assertEqual([(r.number, r.phase) for r in snap.active_rounds], [(2, "overdue"), (3, "open")])
        self.assertEqual([r.number for r in snap.overdue_rounds], [2])
        self.assertEqual((snap.active_rounds[1].played, snap.active_rounds[1].total), (2, 8))
        self.assertEqual(snap.rounds_closed, 1)
        self.assertEqual(snap.rounds_total, 4)
        self.assertEqual((snap.season_played, snap.season_total), (16, 32))
        self.assertEqual(snap.next_round.number, 4)
        self.assertFalse(snap.idle)

    def test_round_without_row_counts_as_scheduled(self):
        rows = _rows(counts=[{"division_id": 2, "round_number": 1, "total": 8, "played": 0}])
        snap = ovw.build_snapshots(self.DIVS[1:], rows, [], NOW, MAX_WARNS)[0]
        self.assertEqual(snap.active_rounds, [])
        self.assertEqual(snap.next_round.number, 1)
        self.assertTrue(snap.idle)

    def test_null_division_belongs_to_division_1(self):
        rows = _rows(
            rounds=[{"division_id": None, "round_number": 1, "is_open": 1, "deadline": _dl(datetime.timedelta(days=1))}],
            warned=[{"division_id": None, "username": "x", "team_name": "Бетис", "warn_count": 1}],
        )
        snaps = ovw.build_snapshots(self.DIVS, rows, [{"division_id": None, "hours_to_escalation": 30}], NOW, MAX_WARNS)
        self.assertEqual(len(snaps[0].active_rounds), 1)
        self.assertEqual(len(snaps[0].debts), 1)
        self.assertEqual(len(snaps[0].warned), 1)
        self.assertEqual((snaps[1].active_rounds, snaps[1].debts, snaps[1].warned), ([], [], []))

    def test_debts_split_into_escalated_and_soon(self):
        debts = [
            {"division_id": 1, "hours_overdue": 60, "hours_to_escalation": -12, "debt": {"state": "active"}},
            {"division_id": 1, "hours_overdue": 20, "hours_to_escalation": 28, "debt": {"state": "escalated"}},
            {"division_id": 1, "hours_overdue": 40, "hours_to_escalation": 8, "debt": None},
            {"division_id": 1, "hours_overdue": 2, "hours_to_escalation": 46, "debt": None},
            {"division_id": 2, "hours_overdue": 2, "hours_to_escalation": 46, "debt": None},
        ]
        snap = ovw.build_snapshots(self.DIVS[:1], _rows(), debts, NOW, MAX_WARNS)[0]
        self.assertEqual(len(snap.debts), 4)
        self.assertEqual(snap.escalated, 2)
        self.assertEqual(snap.escalating_soon, 1)
        # Самый давний долг — первым.
        self.assertEqual([d["hours_overdue"] for d in snap.debts], [60, 40, 20, 2])

    def test_players_one_warn_from_the_limit_are_flagged(self):
        warned = [
            {"division_id": 1, "username": "a", "team_name": "Бетис", "warn_count": 3},
            {"division_id": 1, "username": "b", "team_name": "Порту", "warn_count": 1},
            {"division_id": 1, "username": "c", "team_name": "Брайтон", "warn_count": 4},
        ]
        snap = ovw.build_snapshots(self.DIVS[:1], _rows(warned=warned), [], NOW, MAX_WARNS)[0]
        self.assertEqual(len(snap.warned), 3)
        self.assertEqual({u["username"] for u in snap.at_limit}, {"a", "c"})


class TestRender(unittest.TestCase):
    def _snap(self, **kw):
        return ovw.DivisionSnapshot(id=1, name="Дивизион <1>", **kw)

    def test_summary_lists_divisions_and_attention(self):
        overdue = ovw.RoundProgress(2, "overdue", NOW - datetime.timedelta(hours=3), 6, 8)
        opened = ovw.RoundProgress(3, "open", NOW + datetime.timedelta(days=2, hours=5), 2, 8)
        snap = self._snap(
            active_rounds=[overdue, opened], rounds_total=3, season_played=8, season_total=16,
            debts=[{"hours_to_escalation": -1}], escalated=1,
            warned=[{"username": "a", "team_name": "Бетис", "warn_count": 3}],
            at_limit=[{"username": "a", "team_name": "Бетис", "warn_count": 3}],
        )
        idle = ovw.DivisionSnapshot(id=2, name="Дивизион 2", next_round=ovw.RoundProgress(1, "scheduled", None, 0, 8),
                                    rounds_total=1, season_total=8)
        text = ovw.render_summary([snap, idle], NOW, MAX_WARNS)

        self.assertIn("Дивизион &lt;1&gt;", text)  # имя экранировано под HTML
        self.assertIn("🔴 Тур 2 · 6/8", text)
        self.assertIn("🟢 Тур 3 · 2/8", text)
        self.assertIn("ещё 2 д 5 ч", text)
        self.assertIn("Требует внимания", text)
        self.assertIn("эскалировано долгов — 1", text)
        self.assertIn("Бетис (@a) — 3/4", text)
        self.assertIn("тур 2 — дедлайн прошёл, не сыграно 2", text)
        self.assertIn("нет открытого тура (следующий — 1)", text)

    def test_calm_summary(self):
        snap = self._snap(active_rounds=[ovw.RoundProgress(1, "open", NOW + datetime.timedelta(hours=5), 0, 8)])
        text = ovw.render_summary([snap], NOW, MAX_WARNS)
        self.assertIn("Всё спокойно", text)
        self.assertIn("Долгов нет", text)

    def test_summary_fits_telegram_limit_for_five_busy_divisions(self):
        snaps = []
        for i in range(1, 6):
            snaps.append(ovw.DivisionSnapshot(
                id=i, name=f"Дивизион {i}",
                active_rounds=[ovw.RoundProgress(n, "overdue", NOW - datetime.timedelta(hours=n), 3, 8) for n in range(1, 4)],
                debts=[{"hours_to_escalation": -1}] * 20, escalated=20,
                warned=[{"username": f"user{i}{j}", "team_name": f"Клуб {i}{j}", "warn_count": 3} for j in range(8)],
                at_limit=[{"username": f"user{i}{j}", "team_name": f"Клуб {i}{j}", "warn_count": 3} for j in range(8)],
            ))
        text = ovw.render_summary(snaps, NOW, MAX_WARNS)
        self.assertLess(len(text), 4096)
        self.assertIn("…и ещё", text)

    def test_division_detail_lists_debts_and_warns(self):
        debt = {"round_number": 2, "player1_team": "Бетис", "p1_username": "a", "player2_team": "Порту",
                "p2_username": None, "hours_overdue": 51.7, "hours_to_escalation": -3.7, "debt": {"state": "escalated"}}
        soon = {"round_number": 3, "player1_team": "A", "p1_username": "x", "player2_team": "B", "p2_username": "y",
                "hours_overdue": 40, "hours_to_escalation": 8, "debt": None, "is_extended": 1}
        snap = self._snap(debts=[debt, soon], warned=[{"username": "b", "team_name": "Порту", "warn_count": 1}])
        text = ovw.render_division(snap, NOW, MAX_WARNS)
        self.assertIn("Долги (2)", text)
        self.assertIn("⚡ Тур 2: Бетис (@a) — Порту · 51 ч · эскалирован", text)
        self.assertIn("⏳ Тур 3: A (@x) — B (@y) · 40 ч · эскалация через 8 ч · ❄️ заморожен", text)
        self.assertIn("🟧 Порту (@b) — 1/4", text)

    def test_time_left(self):
        self.assertEqual(ovw.time_left(datetime.timedelta(days=2, hours=5, minutes=10)), "2 д 5 ч")
        self.assertEqual(ovw.time_left(datetime.timedelta(days=1)), "1 д")
        self.assertEqual(ovw.time_left(datetime.timedelta(hours=5, minutes=59)), "5 ч")
        self.assertEqual(ovw.time_left(datetime.timedelta(seconds=10)), "1 мин")

    def test_progress_bar(self):
        self.assertEqual(ovw.progress_bar(0, 0), "░" * 8)
        self.assertEqual(ovw.progress_bar(4, 8), "▓" * 4 + "░" * 4)
        self.assertEqual(ovw.progress_bar(8, 8), "▓" * 8)


class TestOverviewRows(unittest.TestCase):
    """`get_league_overview_rows` на временной базе модуля."""

    @classmethod
    def setUpClass(cls):
        now = now_msk()
        future = (now + datetime.timedelta(days=2)).strftime("%d.%m.%Y %H:%M")
        past = (now - datetime.timedelta(days=3)).strftime("%d.%m.%Y %H:%M")
        with database.transaction() as conn:
            for div in (1, 2):
                conn.execute(
                    "INSERT OR IGNORE INTO divisions (id, name, code, tournament_id) VALUES (?, ?, ?, 1)",
                    (div, f"Дивизион {div}", f"DIV_{div}"),
                )
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id, warn_count) VALUES (9101, 'ov_a', 'Бетис', 1, 3)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id, warn_count) VALUES (9102, 'ov_b', 'Порту', 1, 0)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id, warn_count) VALUES (9201, 'ov_c', 'Брайтон', 2, 1)")
            conn.execute("INSERT INTO users (telegram_id, username, team_name, division_id, warn_count) VALUES (9202, 'ov_d', NULL, 2, 2)")

            conn.execute("INSERT INTO rounds (round_number, is_open, status, deadline, division_id) VALUES (1, 1, 'open', ?, 1)", (past,))
            conn.execute("INSERT INTO rounds (round_number, is_open, status, deadline, division_id) VALUES (2, 1, 'open', ?, 1)", (future,))
            conn.execute("INSERT INTO rounds (round_number, is_open, status, deadline, division_id) VALUES (1, 0, 'scheduled', NULL, 2)")

            matches = [
                (1, "Бетис", "Порту", "confirmed", 1, "league"),
                (1, "Порту", "Бетис", "pending", 1, "league"),
                (1, "Порту", "Бетис", "cancelled", 1, "league"),
                (2, "Бетис", "Порту", "finished", 1, None),
                (2, "Бетис", "Порту", "pending", 1, "cup"),
                (1, "Брайтон", "Порту", "pending", 2, "league"),
            ]
            for rn, t1, t2, status, div, ttype in matches:
                conn.execute(
                    "INSERT INTO matches (round_number, player1_team, player2_team, status, division_id, tournament_type) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rn, t1, t2, status, div, ttype),
                )

    def test_rows(self):
        rows = database.get_league_overview_rows()
        counts = {(c["division_id"], c["round_number"]): (c["played"], c["total"]) for c in rows["match_counts"]}
        # Отменённый и кубковый матчи не считаются; tournament_type NULL — это лига.
        self.assertEqual(counts[(1, 1)], (1, 2))
        self.assertEqual(counts[(1, 2)], (1, 1))
        self.assertEqual(counts[(2, 1)], (0, 1))

        rounds = {(r.get("division_id") or 1, r["round_number"]) for r in rows["rounds"]}
        self.assertTrue({(1, 1), (1, 2), (2, 1)} <= rounds)

        warned = {u["username"]: (u["division_id"], u["warn_count"]) for u in rows["warned_users"]}
        self.assertEqual(warned.get("ov_a"), (1, 3))
        self.assertEqual(warned.get("ov_c"), (2, 1))
        self.assertNotIn("ov_b", warned)  # без варнов
        self.assertNotIn("ov_d", warned)  # без клуба

    def test_snapshot_end_to_end(self):
        divisions = [{"id": 1, "name": "Дивизион 1"}, {"id": 2, "name": "Дивизион 2"}]
        snaps = handler._load_snapshots(divisions)
        d1, d2 = snaps
        self.assertEqual([(r.number, r.phase) for r in d1.active_rounds], [(1, "overdue"), (2, "open")])
        self.assertEqual(len(d1.debts), 1)  # несыгранный матч тура 1 после дедлайна
        self.assertEqual([u["username"] for u in d1.at_limit], ["ov_a"])
        self.assertTrue(d2.idle)
        self.assertEqual(d2.debts, [])


def _update(user_id, data=None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_message.reply_text = AsyncMock()
    if data is None:
        update.callback_query = None
    else:
        update.callback_query.data = data
        update.callback_query.answer = AsyncMock()
        update.callback_query.edit_message_text = AsyncMock()
    return update


class TestAccess(unittest.IsolatedAsyncioTestCase):
    DIVS = [{"id": 1, "name": "Дивизион 1", "code": "DIV_1"}, {"id": 2, "name": "Дивизион 2", "code": "DIV_2"}]

    def _patches(self, global_admin, admin_divs):
        return (
            patch.object(handler, "is_global_admin", return_value=global_admin),
            patch.object(handler.database, "get_active_divisions", return_value=self.DIVS),
            patch.object(handler.database, "get_admin_divisions", return_value=admin_divs),
        )

    def _callbacks(self, markup):
        return [b.callback_data for row in markup.inline_keyboard for b in row]

    async def test_global_admin_sees_every_division(self):
        update = _update(1)
        p1, p2, p3 = self._patches(True, [])
        with p1, p2, p3:
            await handler.overview_command(update, MagicMock())
        kwargs = update.effective_message.reply_text.call_args.kwargs
        self.assertEqual(kwargs["parse_mode"], "HTML")
        callbacks = self._callbacks(kwargs["reply_markup"])
        self.assertIn("ovw_div:1", callbacks)
        self.assertIn("ovw_div:2", callbacks)
        self.assertIn("ovw_home", callbacks)

    async def test_division_admin_sees_only_own_division(self):
        update = _update(2)
        p1, p2, p3 = self._patches(False, [dict(self.DIVS[1], is_active=1)])
        with p1, p2, p3:
            await handler.overview_command(update, MagicMock())
        callbacks = self._callbacks(update.effective_message.reply_text.call_args.kwargs["reply_markup"])
        self.assertIn("ovw_div:2", callbacks)
        self.assertNotIn("ovw_div:1", callbacks)

    async def test_non_admin_is_denied(self):
        update = _update(3)
        p1, p2, p3 = self._patches(False, [])
        with p1, p2, p3:
            await handler.overview_command(update, MagicMock())
        args = update.effective_message.reply_text.call_args.args
        self.assertIn("⛔", args[0])

    async def test_division_admin_cannot_open_foreign_division(self):
        update = _update(2, "ovw_div:1")
        p1, p2, p3 = self._patches(False, [dict(self.DIVS[1], is_active=1)])
        with p1, p2, p3:
            await handler.overview_division_callback(update, MagicMock())
        update.callback_query.answer.assert_awaited_once()
        self.assertTrue(update.callback_query.answer.call_args.kwargs.get("show_alert"))
        update.callback_query.edit_message_text.assert_not_awaited()

    async def test_division_detail_links_to_existing_screens(self):
        update = _update(1, "ovw_div:2")
        p1, p2, p3 = self._patches(True, [])
        with p1, p2, p3:
            await handler.overview_division_callback(update, MagicMock())
        kwargs = update.callback_query.edit_message_text.call_args.kwargs
        callbacks = self._callbacks(kwargs["reply_markup"])
        self.assertEqual(callbacks, ["admin_div_overdue:2", "admin_div_panel:2", "ovw_div:2", "ovw_home"])


class TestMaxWarnsConstant(unittest.TestCase):
    def test_limit_matches_config(self):
        self.assertEqual(MAX_WARNS, config.MAX_WARNS_LIMIT)


if __name__ == "__main__":
    unittest.main()
