"""
FIX-09 — логика рабочих Telegram-топиков дивизиона.

Проверяются шесть рабочих топиков (АНАЛИТИКА, ЧЕРНОВИК, ПРЕДЫ, РЕЗУЛЬТАТЫ,
ОТЧЁТЫ, СОСТАВЫ) на реальных хендлерах и реальных функциях database:
роутинг `group_chat_id + message_thread_id → division_id`, изоляция между
топиками, дивизионами и сезонами, RBAC и поведение на неизвестном топике.

ФЛУДИЛКА и General намеренно не проверяются — они не являются рабочими
топиками бота.
"""

import asyncio
import unittest
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import config
import database
from services.topic_cache import topic_cache

import handlers.drafts as drafts
from handlers.admin import (
    _ensure_match_access,
    _post_or_update_debts_for_division,
    _resolve_analytics_topic,
    admin_div_toggle,
    admin_set_div_topic_cmd,
    admin_view_match,
)
from handlers.base import post_league_table_to_reports
from handlers.cabinet import notify_match_confirmed, save_squad_photo


GROUP_CHAT_ID = -1001999000001
FOREIGN_CHAT_ID = -1001999000002

# thread_id рабочих топиков: 1xx — дивизион 1, 2xx — дивизион 2.
TOPIC_TYPES = ["analytics", "draft", "previews", "results", "reports", "lineups"]
D1_THREADS = {t: 100 + i for i, t in enumerate(TOPIC_TYPES, start=1)}
D2_THREADS = {t: 200 + i for i, t in enumerate(TOPIC_TYPES, start=1)}


def _mock_context() -> MagicMock:
    context = MagicMock()
    context.bot = MagicMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    context.bot.send_photo = AsyncMock(return_value=MagicMock(message_id=2))
    context.bot.edit_message_text = AsyncMock()
    context.bot.edit_message_media = AsyncMock()
    context.bot.edit_message_caption = AsyncMock()
    context.bot.delete_message = AsyncMock()
    context.bot.get_file = AsyncMock()
    context.bot_data = {}
    context.user_data = {}
    return context


class TopicsLogicBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database.init_db()
        uid = uuid.uuid4().hex[:6].upper()

        # --- сезоны -------------------------------------------------------
        # Имена без TEST/LAB: get_active_season() такие сезоны отфильтровывает.
        self.season_prev = database.create_season(f"Сезон Прошлый {uid}")
        self.season_cur = database.create_season(f"Сезон Текущий {uid}")
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("UPDATE seasons SET status = 'finished' WHERE id = ?", (self.season_prev,))
            c.execute("UPDATE seasons SET status = 'archived' WHERE id NOT IN (?, ?)",
                      (self.season_prev, self.season_cur))
            c.execute("UPDATE seasons SET status = 'active' WHERE id = ?", (self.season_cur,))
        self.assertEqual(int(database.get_active_season()), self.season_cur)

        # --- дивизионы ----------------------------------------------------
        self.div1 = database.create_division(name="Дивизион Один", code=f"T9A_{uid}")
        self.div2 = database.create_division(name="Дивизион Два", code=f"T9B_{uid}")

        # --- участники и админы -------------------------------------------
        self.super_admin = 970001
        self.admin_d1 = 970002
        self.admin_d2 = 970003
        self.player_d1a = 970011
        self.player_d1b = 970012
        self.player_d2a = 970021
        self.player_d2b = 970022
        self.plain_user = 970099

        self.team_d1a = f"Клуб A{uid}"
        self.team_d1b = f"Клуб B{uid}"
        self.team_d2a = f"Клуб C{uid}"
        self.team_d2b = f"Клуб D{uid}"

        with database.transaction() as conn:
            c = conn.cursor()
            rows = [
                (self.player_d1a, "p_d1a", self.team_d1a, "player", self.div1),
                (self.player_d1b, "p_d1b", self.team_d1b, "player", self.div1),
                (self.player_d2a, "p_d2a", self.team_d2a, "player", self.div2),
                (self.player_d2b, "p_d2b", self.team_d2b, "player", self.div2),
                (self.plain_user, "plain", f"Клуб E{uid}", "player", self.div1),
                (self.admin_d1, "adm_d1", None, "division_admin", self.div1),
                (self.admin_d2, "adm_d2", None, "division_admin", self.div2),
            ]
            c.executemany("""
                INSERT OR REPLACE INTO users (telegram_id, username, team_name, role, division_id)
                VALUES (?, ?, ?, ?, ?)
            """, rows)
        database.add_division_admin(self.div1, self.admin_d1)
        database.add_division_admin(self.div2, self.admin_d2)

        database.set_config("group_id", str(GROUP_CHAT_ID))

        # --- привязки топиков ---------------------------------------------
        for topic_type in TOPIC_TYPES:
            database.set_division_topic(self.div1, topic_type, D1_THREADS[topic_type], GROUP_CHAT_ID)
            database.set_division_topic(self.div2, topic_type, D2_THREADS[topic_type], GROUP_CHAT_ID)
        topic_cache.reload_cache()

        self._admin_ids_patch = patch.object(config, "ADMIN_IDS", [self.super_admin])
        self._admin_ids_patch.start()

    async def asyncTearDown(self):
        self._admin_ids_patch.stop()
        drafts.draft_media_groups.clear()
        for task in list(drafts.draft_tasks.values()):
            task.cancel()
        drafts.draft_tasks.clear()
        topic_cache.reload_cache()

    # --- фабрики апдейтов -------------------------------------------------

    def _group_message_update(self, user_id: int, thread_id: int | None,
                              chat_id: int = GROUP_CHAT_ID, text: str | None = None):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_user.username = "tester"
        update.effective_chat = MagicMock()
        update.effective_chat.id = chat_id
        update.effective_chat.type = "supergroup"
        msg = MagicMock()
        msg.message_id = 555
        msg.message_thread_id = thread_id
        msg.is_topic_message = thread_id is not None
        msg.media_group_id = None
        msg.photo = []
        msg.caption = text
        msg.text = text
        msg.reply_text = AsyncMock()
        update.message = msg
        update.effective_message = msg
        update.callback_query = None
        return update

    def _callback_update(self, user_id: int, callback_data: str):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_chat = MagicMock()
        update.effective_chat.id = user_id
        update.effective_chat.type = "private"
        query = MagicMock()
        query.from_user.id = user_id
        query.data = callback_data
        query.answer = AsyncMock()
        query.edit_message_text = AsyncMock()
        query.edit_message_caption = AsyncMock()
        query.message = MagicMock()
        query.message.message_id = 42
        update.callback_query = query
        update.message = None
        return update

    # --- фабрики данных ---------------------------------------------------

    def _insert_match(self, division_id: int, season_id: int, round_number: int,
                      team1: str, team2: str, status: str = "pending",
                      p1_score=None, p2_score=None) -> int:
        with database.transaction() as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO matches
                    (round_number, player1_team, player2_team, status, division_id, season_id,
                     player1_score, player2_score, tournament_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'league')
            """, (round_number, team1, team2, status, division_id, season_id, p1_score, p2_score))
            match_id = c.lastrowid
            c.execute("""
                INSERT OR IGNORE INTO rounds (season_id, division_id, round_number, is_open, deadline)
                VALUES (?, ?, ?, 1, NULL)
            """, (season_id, division_id, round_number))
        return match_id


class TestTopicRoutingMap(TopicsLogicBase):
    """§3–§4: карта топиков и роутинг chat+thread → дивизион."""

    async def test_each_working_topic_resolves_to_its_own_division(self):
        for topic_type in TOPIC_TYPES:
            for div_id, threads in ((self.div1, D1_THREADS), (self.div2, D2_THREADS)):
                with self.subTest(topic=topic_type, division=div_id):
                    binding = topic_cache.get_by_topic(GROUP_CHAT_ID, threads[topic_type])
                    self.assertIsNotNone(binding)
                    self.assertEqual(binding["division_id"], div_id)
                    self.assertEqual(binding["topic_type"], topic_type)

                    div = database.get_division_by_topic(threads[topic_type], topic_type, GROUP_CHAT_ID)
                    self.assertIsNotNone(div)
                    self.assertEqual(div["id"], div_id)

    async def test_topic_threads_are_unique_across_topics_and_divisions(self):
        """§11: ни один thread_id не обслуживает два топика или два дивизиона."""
        seen = {}
        for div_id, threads in ((self.div1, D1_THREADS), (self.div2, D2_THREADS)):
            for topic_type, tid in threads.items():
                self.assertNotIn(tid, seen, f"thread {tid} привязан дважды: {seen.get(tid)}")
                seen[tid] = (div_id, topic_type)
                binding = topic_cache.get_by_topic(GROUP_CHAT_ID, tid)
                self.assertEqual((binding["division_id"], binding["topic_type"]), (div_id, topic_type))

    async def test_foreign_topic_type_does_not_match_binding(self):
        """§4: тред РЕЗУЛЬТАТОВ не резолвится как ЧЕРНОВИК."""
        self.assertIsNone(
            database.get_division_by_topic(D1_THREADS["results"], "draft", GROUP_CHAT_ID)
        )
        self.assertIsNone(
            database.get_division_by_topic(D2_THREADS["lineups"], "reports", GROUP_CHAT_ID)
        )

    async def test_unknown_thread_and_foreign_chat_fail_safe(self):
        """§16: неизвестный топик и чужая группа не должны попадать в дивизион."""
        self.assertIsNone(topic_cache.get_by_topic(GROUP_CHAT_ID, 987654))
        self.assertIsNone(topic_cache.get_by_topic(FOREIGN_CHAT_ID, D1_THREADS["draft"]))
        self.assertIsNone(topic_cache.get_by_topic(None, D1_THREADS["draft"]))
        self.assertIsNone(
            database.get_division_by_topic(D1_THREADS["draft"], "draft", FOREIGN_CHAT_ID)
        )


class TestDraftTopic(TopicsLogicBase):
    """§6: ЧЕРНОВИК."""

    def _buffer_for(self, update) -> dict | None:
        key = f"user_{update.effective_chat.id}_{update.message.message_thread_id}_{update.effective_user.id}"
        return drafts.draft_media_groups.get(key)

    async def test_draft_in_own_topic_binds_its_division(self):
        for div_id, threads, user in (
            (self.div1, D1_THREADS, self.player_d1a),
            (self.div2, D2_THREADS, self.player_d2a),
        ):
            with self.subTest(division=div_id):
                update = self._group_message_update(user, threads["draft"], text="1 тур")
                await drafts.handle_draft_media(update, _mock_context())
                buf = self._buffer_for(update)
                self.assertIsNotNone(buf)
                self.assertEqual(buf["division_id"], div_id)

    async def test_legacy_drafts_topic_id_does_not_override_division_binding(self):
        """
        Регрессия: глобальный drafts_topic_id сравнивался с «голым» thread_id
        раньше поиска привязки и обнулял division_id — черновик дивизиона 2
        уходил в поиск матча по всем дивизионам.
        """
        database.set_config("drafts_topic_id", str(D2_THREADS["draft"]))
        update = self._group_message_update(self.player_d2a, D2_THREADS["draft"], text="1 тур")
        await drafts.handle_draft_media(update, _mock_context())
        buf = self._buffer_for(update)
        self.assertIsNotNone(buf)
        self.assertEqual(buf["division_id"], self.div2)

    async def test_draft_handler_ignores_results_topic(self):
        """§11 ЧЕРНОВИК ← РЕЗУЛЬТАТЫ: чужой топик не создаёт черновик."""
        update = self._group_message_update(self.player_d1a, D1_THREADS["results"], text="1 тур")
        await drafts.handle_draft_media(update, _mock_context())
        self.assertIsNone(self._buffer_for(update))
        self.assertEqual(drafts.draft_media_groups, {})

    async def test_draft_handler_ignores_message_without_thread(self):
        """§16: сообщение без message_thread_id (General) игнорируется."""
        update = self._group_message_update(self.player_d1a, None, text="1 тур")
        await drafts.handle_draft_media(update, _mock_context())
        self.assertEqual(drafts.draft_media_groups, {})

    async def test_draft_confirm_rbac_is_division_scoped(self):
        """§6/§12: черновик дивизиона 1 недоступен админу дивизиона 2."""
        draft = {"games": [{"division_id": self.div1}]}
        self.assertTrue(await drafts._can_manage_draft(self.super_admin, draft))
        self.assertTrue(await drafts._can_manage_draft(self.admin_d1, draft))
        self.assertFalse(await drafts._can_manage_draft(self.admin_d2, draft))
        self.assertFalse(await drafts._can_manage_draft(self.plain_user, draft))

    async def test_draft_confirm_callback_denies_foreign_division_admin(self):
        update = self._callback_update(self.admin_d2, "draft_conf_abc123")
        context = _mock_context()
        context.bot_data["drafts"] = {"abc123": {"games": [{"division_id": self.div1}]}}
        await drafts.cb_draft_confirm(update, context)
        update.callback_query.answer.assert_awaited()
        alert_text = update.callback_query.answer.await_args.args[0]
        self.assertIn("нет прав", alert_text)
        # Черновик не тронут — отклонения не было, только отказ.
        self.assertIn("abc123", context.bot_data["drafts"])

    async def test_draft_reject_callback_denies_foreign_division_admin(self):
        update = self._callback_update(self.admin_d2, "draft_rej_abc123")
        context = _mock_context()
        context.bot_data["drafts"] = {"abc123": {"games": [{"division_id": self.div1}]}}
        await drafts.cb_draft_reject(update, context)
        self.assertIn("abc123", context.bot_data["drafts"])


class TestResultsTopic(TopicsLogicBase):
    """§8: РЕЗУЛЬТАТЫ."""

    async def test_confirmed_result_is_posted_only_to_own_results_topic(self):
        match_id = self._insert_match(self.div1, self.season_cur, 1,
                                      self.team_d1a, self.team_d1b,
                                      status="confirmed", p1_score=2, p2_score=1)
        context = _mock_context()
        await notify_match_confirmed(context, match_id)

        threads = [
            call.kwargs.get("message_thread_id")
            for call in context.bot.send_message.await_args_list
            if call.kwargs.get("chat_id") == GROUP_CHAT_ID
        ]
        self.assertEqual(threads.count(D1_THREADS["results"]), 1)
        # Ни одного сообщения в топики чужого дивизиона.
        self.assertFalse(set(threads) & set(D2_THREADS.values()))

    async def test_match_lookup_respects_division_boundary(self):
        """§8/§12: одна и та же пара клубов может существовать в двух дивизионах."""
        m1 = self._insert_match(self.div1, self.season_cur, 3, self.team_d1a, self.team_d1b)
        m2 = self._insert_match(self.div2, self.season_cur, 3, self.team_d1a, self.team_d1b)

        found1 = database.get_active_match_by_teams(self.team_d1a, self.team_d1b,
                                                    caption="3 тур", division_id=self.div1)
        found2 = database.get_active_match_by_teams(self.team_d1a, self.team_d1b,
                                                    caption="3 тур", division_id=self.div2)
        self.assertIsNotNone(found1)
        self.assertIsNotNone(found2)
        self.assertEqual(found1["id"], m1)
        self.assertEqual(found2["id"], m2)

    async def test_match_lookup_reads_round_state_of_its_own_division(self):
        """
        §8/§12: `rounds` уникален по (season_id, division_id, round_number).
        JOIN только по round_number подтягивал is_open/deadline чужого дивизиона,
        и скоринг кандидатов выбирал не тот матч.
        """
        past = "2020-01-01 00:00:00"
        closed_round = self._insert_match(self.div1, self.season_cur, 20,
                                          self.team_d1a, self.team_d1b)
        open_round = self._insert_match(self.div1, self.season_cur, 30,
                                        self.team_d1a, self.team_d1b)
        with database.transaction() as conn:
            c = conn.cursor()
            # Свой дивизион: тур 20 закрыт, тур 30 открыт с истёкшим дедлайном.
            c.execute("UPDATE rounds SET is_open = 0, deadline = NULL "
                      "WHERE season_id = ? AND division_id = ? AND round_number = 20",
                      (self.season_cur, self.div1))
            c.execute("UPDATE rounds SET is_open = 1, deadline = ? "
                      "WHERE season_id = ? AND division_id = ? AND round_number = 30",
                      (past, self.season_cur, self.div1))
            # Чужой дивизион: состояние туров зеркально противоположное.
            c.execute("""
                INSERT OR REPLACE INTO rounds (season_id, division_id, round_number, is_open, deadline)
                VALUES (?, ?, 20, 1, ?), (?, ?, 30, 0, NULL)
            """, (self.season_cur, self.div2, past, self.season_cur, self.div2))

        found = database.get_active_match_by_teams(self.team_d1a, self.team_d1b,
                                                   caption="", division_id=self.div1)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], open_round)
        self.assertNotEqual(found["id"], closed_round)

    async def test_match_lookup_ignores_other_season(self):
        """§13: незакрытый матч прошлого сезона не должен выигрывать матчинг."""
        self._insert_match(self.div1, self.season_prev, 5, self.team_d1a, self.team_d1b)
        found = database.get_active_match_by_teams(self.team_d1a, self.team_d1b,
                                                   caption="5 тур", division_id=self.div1)
        self.assertIsNone(found)

        current = self._insert_match(self.div1, self.season_cur, 5, self.team_d1a, self.team_d1b)
        found = database.get_active_match_by_teams(self.team_d1a, self.team_d1b,
                                                   caption="5 тур", division_id=self.div1)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], current)

    async def test_round_matches_are_season_scoped(self):
        """§13: номер тура повторяется в каждом сезоне."""
        old = self._insert_match(self.div1, self.season_prev, 7, self.team_d1a, self.team_d1b)
        cur = self._insert_match(self.div1, self.season_cur, 7, self.team_d1a, self.team_d1b)

        ids = {m["id"] for m in database.get_matches_by_round(7, division_id=self.div1)}
        self.assertIn(cur, ids)
        self.assertNotIn(old, ids)

        old_ids = {m["id"] for m in database.get_matches_by_round(
            7, division_id=self.div1, season_id=self.season_prev)}
        self.assertIn(old, old_ids)
        self.assertNotIn(cur, old_ids)

    async def test_round_matches_are_division_scoped(self):
        """§12: тур дивизиона 1 не содержит матчей дивизиона 2."""
        m1 = self._insert_match(self.div1, self.season_cur, 9, self.team_d1a, self.team_d1b)
        m2 = self._insert_match(self.div2, self.season_cur, 9, self.team_d2a, self.team_d2b)

        ids1 = {m["id"] for m in database.get_matches_by_round(9, division_id=self.div1)}
        ids2 = {m["id"] for m in database.get_matches_by_round(9, division_id=self.div2)}
        self.assertEqual(ids1, {m1})
        self.assertEqual(ids2, {m2})

    async def test_division_admin_cannot_open_foreign_match_card(self):
        """§8/§12 RBAC: карточка чужого матча закрыта для админа другого дивизиона."""
        match_id = self._insert_match(self.div2, self.season_cur, 2, self.team_d2a, self.team_d2b)
        match = database.get_match(match_id)

        deny_update = self._callback_update(self.admin_d1, f"admin_view_match_{match_id}")
        self.assertFalse(await _ensure_match_access(deny_update, match))

        allow_update = self._callback_update(self.admin_d2, f"admin_view_match_{match_id}")
        self.assertTrue(await _ensure_match_access(allow_update, match))

        super_update = self._callback_update(self.super_admin, f"admin_view_match_{match_id}")
        self.assertTrue(await _ensure_match_access(super_update, match))

    async def test_admin_view_match_denies_foreign_division_admin(self):
        match_id = self._insert_match(self.div2, self.season_cur, 2, self.team_d2a, self.team_d2b)
        update = self._callback_update(self.admin_d1, f"admin_view_match_{match_id}")
        await admin_view_match(update, _mock_context())

        alerts = [c for c in update.callback_query.answer.await_args_list if c.args]
        self.assertTrue(alerts, "ожидался alert с отказом в доступе")
        self.assertIn("нет прав", alerts[-1].args[0])
        update.callback_query.edit_message_text.assert_not_awaited()


class TestReportsTopic(TopicsLogicBase):
    """§9: ОТЧЁТЫ."""

    async def test_league_table_goes_to_own_reports_topic(self):
        context = _mock_context()
        with patch("handlers.base.generate_league_table_image", return_value=b"img"):
            await post_league_table_to_reports(context, division_id=self.div2)

        context.bot.send_photo.assert_awaited()
        kwargs = context.bot.send_photo.await_args.kwargs
        self.assertEqual(kwargs["chat_id"], GROUP_CHAT_ID)
        self.assertEqual(kwargs["message_thread_id"], D2_THREADS["reports"])

    async def test_league_table_without_division_updates_every_active_division(self):
        """
        Регрессия: вызов без division_id (подтверждение результата, кнопка
        «Обновить таблицы») молча выходил, и таблица в ОТЧЁТАХ не обновлялась.
        """
        context = _mock_context()
        with patch("handlers.base.generate_league_table_image", return_value=b"img"):
            await post_league_table_to_reports(context)

        threads = {c.kwargs.get("message_thread_id") for c in context.bot.send_photo.await_args_list}
        self.assertIn(D1_THREADS["reports"], threads)
        self.assertIn(D2_THREADS["reports"], threads)

    async def test_reports_topic_is_not_the_lineups_topic(self):
        """§11 ОТЧЁТЫ ← СОСТАВЫ."""
        context = _mock_context()
        with patch("handlers.base.generate_league_table_image", return_value=b"img"):
            await post_league_table_to_reports(context, division_id=self.div1)
        kwargs = context.bot.send_photo.await_args.kwargs
        self.assertNotEqual(kwargs["message_thread_id"], D1_THREADS["lineups"])
        self.assertNotEqual(kwargs["message_thread_id"], D2_THREADS["reports"])


class TestAnalyticsTopic(TopicsLogicBase):
    """§5: АНАЛИТИКА."""

    async def test_analytics_topic_resolves_per_division(self):
        self.assertEqual(
            await _resolve_analytics_topic(self.div1),
            (GROUP_CHAT_ID, D1_THREADS["analytics"]),
        )
        self.assertEqual(
            await _resolve_analytics_topic(self.div2),
            (GROUP_CHAT_ID, D2_THREADS["analytics"]),
        )

    async def test_analytics_topic_absent_returns_none(self):
        """§16: без привязки бот не должен писать в чужой топик."""
        div3 = database.create_division(name="Дивизион Три", code=f"T9C_{uuid.uuid4().hex[:6].upper()}")
        topic_cache.reload_cache()
        self.assertIsNone(await _resolve_analytics_topic(div3))

    async def test_analytics_payload_is_division_and_season_scoped(self):
        """§5/§13: превью тура берёт матчи только своего дивизиона и сезона."""
        from services import round_preview

        cur = self._insert_match(self.div1, self.season_cur, 4, self.team_d1a, self.team_d1b)
        self._insert_match(self.div1, self.season_prev, 4, self.team_d1a, self.team_d1b)
        self._insert_match(self.div2, self.season_cur, 4, self.team_d2a, self.team_d2b)

        payload = round_preview.build_preview_payload(self.div1, 4, season_id=self.season_cur)
        self.assertEqual([f["match_id"] for f in payload["fixtures"]], [cur])
        self.assertEqual(payload["division_id"], self.div1)


class TestPreviewsTopic(TopicsLogicBase):
    """§7: ПРЕДЫ."""

    async def test_debts_summary_goes_to_own_previews_topic(self):
        self._insert_match(self.div1, self.season_cur, 1, self.team_d1a, self.team_d1b)
        # Долг возникает от дедлайна: открытый тур без дедлайна долгов не даёт.
        with database.transaction() as conn:
            conn.execute("UPDATE rounds SET deadline = '2020-01-01 00:00:00' "
                         "WHERE season_id = ? AND division_id = ? AND round_number = 1",
                         (self.season_cur, self.div1))
        context = _mock_context()

        ok, _ = await _post_or_update_debts_for_division(context, self.div1, "Дивизион Один")
        self.assertTrue(ok)
        sent = [c for c in context.bot.send_message.await_args_list
                if c.kwargs.get("chat_id") == GROUP_CHAT_ID]
        self.assertTrue(sent)
        for call in sent:
            self.assertEqual(call.kwargs["message_thread_id"], D1_THREADS["previews"])

    async def test_previews_topic_lookup_is_division_scoped(self):
        """§7/§11 ПРЕДЫ ← РЕЗУЛЬТАТЫ: тред ПРЕДОВ не совпадает с чужими."""
        self.assertEqual(
            database.get_division_topic(self.div1, "previews", GROUP_CHAT_ID),
            D1_THREADS["previews"],
        )
        self.assertEqual(
            database.get_division_topic(self.div2, "previews", GROUP_CHAT_ID),
            D2_THREADS["previews"],
        )
        self.assertNotEqual(
            database.get_division_topic(self.div1, "previews", GROUP_CHAT_ID),
            database.get_division_topic(self.div1, "results", GROUP_CHAT_ID),
        )


class TestLineupsTopic(TopicsLogicBase):
    """§10: СОСТАВЫ."""

    def _private_photo_update(self, user_id: int):
        update = MagicMock()
        update.effective_user = MagicMock()
        update.effective_user.id = user_id
        update.effective_user.username = "squadman"
        update.effective_chat = MagicMock()
        update.effective_chat.id = user_id
        update.effective_chat.type = "private"
        photo = MagicMock()
        photo.file_id = "photo_file_id"
        msg = MagicMock()
        msg.photo = [photo]
        msg.reply_text = AsyncMock()
        update.message = msg
        update.callback_query = None
        return update

    async def _run_save_squad(self, user_id: int):
        update = self._private_photo_update(user_id)
        context = _mock_context()
        with patch("handlers.cabinet.show_my_squad", new=AsyncMock()), \
             patch("handlers.squad_ai.offer_recognized_squad", new=AsyncMock()):
            await save_squad_photo(update, context)
        return context

    async def test_squad_photo_goes_to_own_lineups_topic(self):
        context = await self._run_save_squad(self.player_d2a)
        topic_calls = [c for c in context.bot.send_photo.await_args_list
                       if c.kwargs.get("chat_id") == GROUP_CHAT_ID]
        self.assertEqual(len(topic_calls), 1)
        self.assertEqual(topic_calls[0].kwargs["message_thread_id"], D2_THREADS["lineups"])

    async def test_squad_photo_never_reaches_other_division_or_results(self):
        """§11 СОСТАВЫ ← РЕЗУЛЬТАТЫ и §12."""
        context = await self._run_save_squad(self.player_d1a)
        threads = {c.kwargs.get("message_thread_id") for c in context.bot.send_photo.await_args_list}
        self.assertEqual(threads, {D1_THREADS["lineups"]})
        self.assertNotIn(D2_THREADS["lineups"], threads)
        self.assertNotIn(D1_THREADS["results"], threads)


class TestTopicBindingCommandRbac(TopicsLogicBase):
    """§14–§15: команда /set_div_topic и админские экраны дивизионов."""

    async def test_set_div_topic_binds_chat_and_updates_cache(self):
        new_thread = 30777
        update = self._group_message_update(self.admin_d1, new_thread, text="/set_div_topic")
        context = _mock_context()
        context.args = [str(self.div1), "results"]

        await admin_set_div_topic_cmd(update, context)

        self.assertEqual(
            database.get_division_topic(self.div1, "results", GROUP_CHAT_ID), new_thread
        )
        topics_map = database.get_division_topics_map(self.div1)
        self.assertEqual(topics_map["results"]["group_chat_id"], GROUP_CHAT_ID)
        # Кэш обязан быть синхронизирован сразу, без рестарта бота.
        binding = topic_cache.get_by_topic(GROUP_CHAT_ID, new_thread)
        self.assertIsNotNone(binding)
        self.assertEqual(binding["division_id"], self.div1)
        self.assertEqual(binding["topic_type"], "results")

    async def test_set_div_topic_denied_for_foreign_division_admin(self):
        update = self._group_message_update(self.admin_d2, 30888, text="/set_div_topic")
        context = _mock_context()
        context.args = [str(self.div1), "results"]

        await admin_set_div_topic_cmd(update, context)

        self.assertEqual(
            database.get_division_topic(self.div1, "results", GROUP_CHAT_ID),
            D1_THREADS["results"],
        )
        self.assertIsNone(topic_cache.get_by_topic(GROUP_CHAT_ID, 30888))
        update.message.reply_text.assert_awaited()
        self.assertIn("нет прав", update.message.reply_text.await_args.args[0])

    async def test_set_div_topic_denied_for_plain_user(self):
        update = self._group_message_update(self.plain_user, 30999, text="/set_div_topic")
        context = _mock_context()
        context.args = [str(self.div1), "results"]

        await admin_set_div_topic_cmd(update, context)

        self.assertIsNone(topic_cache.get_by_topic(GROUP_CHAT_ID, 30999))
        self.assertIn("нет прав доступа", update.message.reply_text.await_args.args[0])

    async def test_set_div_topic_requires_thread(self):
        """§16: вне топика команда ничего не привязывает."""
        update = self._group_message_update(self.super_admin, None, text="/set_div_topic")
        context = _mock_context()
        context.args = [str(self.div1), "results"]

        await admin_set_div_topic_cmd(update, context)

        self.assertEqual(
            database.get_division_topic(self.div1, "results", GROUP_CHAT_ID),
            D1_THREADS["results"],
        )
        self.assertIn("внутри нужного форум-топика", update.message.reply_text.await_args.args[0])

    async def test_division_management_is_super_admin_only(self):
        """§12: админ дивизиона не управляет настройками дивизионов."""
        update = self._callback_update(self.admin_d1, f"admin_div_toggle_{self.div2}")
        await admin_div_toggle(update, _mock_context())
        self.assertEqual(database.get_division(self.div2)["is_active"], 1)

        update_own = self._callback_update(self.admin_d1, f"admin_div_toggle_{self.div1}")
        await admin_div_toggle(update_own, _mock_context())
        self.assertEqual(database.get_division(self.div1)["is_active"], 1)

        update_super = self._callback_update(self.super_admin, f"admin_div_toggle_{self.div2}")
        with patch("handlers.admin.admin_div_view", new=AsyncMock()):
            await admin_div_toggle(update_super, _mock_context())
        self.assertEqual(database.get_division(self.div2)["is_active"], 0)


if __name__ == "__main__":
    unittest.main()
