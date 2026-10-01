"""Журнал действий админов: log_admin_action, get_admin_journal, services/admin_journal.py
и доступ к экранам handlers/admin_ops.py."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import database
from handlers import admin_ops
from services import admin_journal

ADMIN_WITH_ROW = 880001
ADMIN_WITHOUT_ROW = 880002
OTHER_ADMIN = 880003


@pytest.fixture(scope="module", autouse=True)
def _users():
    with database.transaction() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO users (telegram_id, username, role) VALUES (?, ?, ?)",
            [(ADMIN_WITH_ROW, "JournalAdmin", "admin"), (OTHER_ADMIN, "other_admin", "admin")],
        )


@pytest.fixture(autouse=True)
def _clean_logs():
    with database.transaction() as conn:
        conn.execute("DELETE FROM admin_audit_log")
        conn.execute("DELETE FROM bet_audit_log")
    yield


def _journal(**kwargs):
    rows, total = database.get_admin_journal(50, 0, **kwargs)
    return rows, total


class TestLogAdminAction:
    def test_admin_with_users_row_goes_to_admin_audit_log(self):
        database.log_admin_action(ADMIN_WITH_ROW, "warn_added", "user", 5, old_value="0/4",
                                  new_value="1/4", reason="неявка", division_id=2)
        rows, total = _journal()
        assert total == 1
        row = rows[0]
        assert row["src"] == "admin"
        assert row["actor_id"] == ADMIN_WITH_ROW
        assert row["actor_username"] == "JournalAdmin"
        assert (row["old_value"], row["new_value"], row["reason"]) == ("0/4", "1/4", "неявка")
        assert row["division_id"] == 2

    def test_admin_without_users_row_falls_back_to_bet_audit_log(self):
        database.log_admin_action(ADMIN_WITHOUT_ROW, "round_closed", "round", 7,
                                  new_value="тур 3", reason="досрочно")
        with database.transaction() as conn:
            assert conn.execute("SELECT COUNT(*) FROM admin_audit_log").fetchone()[0] == 0
        rows, total = _journal()
        assert total == 1
        row = rows[0]
        assert row["src"] == "bet"
        assert row["actor_id"] == ADMIN_WITHOUT_ROW
        assert row["actor_username"] is None
        assert row["new_value"] == "тур 3 — досрочно"

    def test_reason_only_becomes_new_value_in_fallback(self):
        database.log_admin_action(ADMIN_WITHOUT_ROW, "warn_removed", "user", 1, reason="/unwarn")
        rows, _ = _journal()
        assert rows[0]["new_value"] == "/unwarn"


class TestGetAdminJournal:
    def _seed(self):
        database.log_admin_action(ADMIN_WITH_ROW, "warn_added", "user", 1)
        database.log_admin_action(ADMIN_WITH_ROW, "odds_changed", "selection", 2)
        database.log_admin_action(OTHER_ADMIN, "round_opened", "round", 3)
        database.log_admin_action(ADMIN_WITHOUT_ROW, "club_bound", "user", 4)
        database.log_admin_action(0, "live_market_void", "market", 5)  # system actor

    def test_system_actor_is_hidden(self):
        self._seed()
        rows, total = _journal()
        assert total == 4
        assert all(r["actor_id"] > 0 for r in rows)

    def test_action_filters(self):
        self._seed()
        rows, total = _journal(actions=["warn_added", "round_opened"])
        assert total == 2
        assert {r["action"] for r in rows} == {"warn_added", "round_opened"}
        rows, total = _journal(exclude_actions=["odds_changed"])
        assert total == 3
        assert "odds_changed" not in {r["action"] for r in rows}

    def test_actor_filter_and_paging(self):
        self._seed()
        rows, total = _journal(actor_id=ADMIN_WITH_ROW)
        assert total == 2 and {r["actor_id"] for r in rows} == {ADMIN_WITH_ROW}
        page, total = database.get_admin_journal(1, 1)
        assert total == 4 and len(page) == 1

    def test_find_telegram_id_by_username(self):
        assert database.find_telegram_id_by_username("@journaladmin") == ADMIN_WITH_ROW
        assert database.find_telegram_id_by_username("JOURNALADMIN") == ADMIN_WITH_ROW
        assert database.find_telegram_id_by_username("@nobody_here") is None
        assert database.find_telegram_id_by_username("") is None


class TestJournalModule:
    def test_every_action_has_a_known_category(self):
        for action, (category, label) in admin_journal.ACTIONS.items():
            assert category in admin_journal.CATEGORIES, action
            assert label

    def test_filters(self):
        assert admin_journal.journal_filter(None) == {
            "exclude_actions": list(admin_journal.NOISY_ACTIONS)}
        assert admin_journal.journal_filter("bogus") == admin_journal.journal_filter(None)
        bets = admin_journal.journal_filter("bets")["actions"]
        assert "bet_voided" in bets and "odds_changed" not in bets
        other = admin_journal.journal_filter("other")["exclude_actions"]
        assert "warn_added" in other and "odds_changed" in other

    def test_other_category_catches_uncatalogued_actions(self):
        database.log_admin_action(ADMIN_WITH_ROW, "brand_new_action", "x", 1)
        database.log_admin_action(ADMIN_WITH_ROW, "warn_added", "user", 1)
        rows, total = _journal(**admin_journal.journal_filter("other"))
        assert total == 1 and rows[0]["action"] == "brand_new_action"
        assert admin_journal.action_label("brand_new_action") == "brand_new_action"
        assert admin_journal.action_category("brand_new_action") == "other"

    def test_format_entry(self):
        text = admin_journal.format_entry({
            "created_at": "2026-09-30 21:05:11", "actor_username": "boss", "actor_id": 1,
            "action": "warn_added", "target_type": "user", "target_id": 12, "division_id": 3,
            "old_value": "0/4", "new_value": "1/4", "reason": "<грубость>",
        })
        lines = text.split("\n")
        assert lines[0] == "🕑 30.09 21:05 · @boss"
        assert lines[1] == "<b>Выдан варн</b> · игрок #12 · див. 3"
        assert lines[2].strip() == "0/4 → 1/4"
        assert lines[3].strip() == "💬 &lt;грубость&gt;"

        bare = admin_journal.format_entry({"created_at": "2026-09-30 21:05:11", "actor_id": 77,
                                           "action": "round_closed", "old_value": "open"})
        assert "ID <code>77</code>" in bare and "было: open" in bare

    def test_format_entry_humanizes_values(self):
        voided = admin_journal.format_entry({
            "created_at": "2026-09-30 19:55:00", "actor_username": "Flasin5", "actor_id": 1,
            "action": "market_voided", "target_type": "market", "target_id": 10018,
            "old_value": '{"status": "closed"}', "new_value": '{"status": "voided"}',
        })
        assert "<b>Рынок аннулирован</b> · рынок #10018" in voided
        assert "закрыт → аннулирован" in voided and "{" not in voided

        wallet = admin_journal.format_entry({
            "created_at": "2026-09-30 19:55:00", "actor_id": 1, "action": "wallet_admin_credit",
            "target_type": "user", "target_id": 5, "old_value": '{"balance": 677}',
            "new_value": '{"balance": 777, "amount": 100, "reason": "приз"}',
        })
        assert "баланс: 677 → 777 · сумма: 100 · причина: приз" in wallet

        limit = admin_journal.format_entry({
            "created_at": "2026-09-30 19:55:00", "actor_id": 1, "action": "limit_set",
            "target_type": "risk_limit", "target_id": 3, "division_id": 3,
            "old_value": '{"scope_type": "division", "limit_key": "max_bet", "value": null}',
            "new_value": '{"scope_type": "division", "limit_key": "max_bet", "value": 500}',
        })
        assert "<b>Установлен лимит</b> · див. 3" in limit
        assert "область: дивизион · лимит: макс. ставка · значение: — → 500" in limit

        refund = admin_journal.format_entry({
            "created_at": "2026-09-30 19:55:00", "actor_id": 1, "action": "bet_voided",
            "target_type": "bet", "target_id": 9, "old_value": '{"status": "pending", "amount": 50}',
            "new_value": '{"status": "refunded", "refund": 50}',
        })
        assert "статус: в игре → возвращена" in refund

        backup = admin_journal.format_entry({
            "created_at": "2026-10-01 02:25:00", "actor_id": 1, "action": "db_backup_created",
            "target_type": "backup", "new_value": "league-20261001-022532.db.gz",
        })
        assert "backup" not in backup.split("\n")[1]

        deadline = admin_journal.format_entry({
            "created_at": "2026-10-01 02:25:00", "actor_id": 1, "action": "match_deadline_extended",
            "target_type": "match", "target_id": 5388, "new_value": "+24 ч, до 2026-10-02 02:25:44",
        })
        assert "матч #5388" in deadline and "+24 ч, до 02.10 02:25" in deadline

        unknown = admin_journal.format_entry({"created_at": "2026-10-01 02:25:00", "actor_id": 1,
                                              "action": "brand_new_action"})
        assert "<code>brand_new_action</code>" in unknown

    def test_group_entries_merges_one_batch(self):
        def row(target_id, minute="19:55:01", actor=1, new='{"status": "voided"}'):
            return {"created_at": f"2026-09-30 {minute}", "actor_id": actor, "actor_username": "a",
                    "action": "market_voided", "target_type": "market", "target_id": target_id,
                    "old_value": '{"status": "closed"}', "new_value": new}

        rows = [row(i) for i in range(10018, 10012, -1)]
        rows += [row(10396, minute="19:14:00"), row(10395, minute="19:14:30"),
                 row(500, minute="19:14:30", actor=2)]
        groups = admin_journal.group_entries(rows)
        assert [g["count"] for g in groups] == [6, 2, 1]
        text = admin_journal.format_entry(groups[0])
        assert "<b>Рынок аннулирован</b> ×6 · рынки #10013–#10018" in text
        assert "рынки #10395, #10396" in admin_journal.format_entry(groups[1])
        # Different values are not one batch.
        assert len(admin_journal.group_entries([row(1), row(2, new='{"status": "open"}')])) == 2

    def test_record_writes_and_skips_missing_actor(self):
        asyncio.run(admin_journal.record(ADMIN_WITH_ROW, "match_reset", "match", 9,
                                         old="2:1", new=None, reason="x" * 600))
        asyncio.run(admin_journal.record(None, "match_reset", "match", 10))
        asyncio.run(admin_journal.record(0, "match_reset", "match", 11))
        rows, total = _journal()
        assert total == 1
        assert rows[0]["target_id"] == 9
        assert len(rows[0]["reason"]) == 498 and rows[0]["reason"].endswith("…")


# ─── Доступ к экранам admin_ops ──────────────────────────────────────────────

def _update(chat_type="private", user_id=ADMIN_WITH_ROW, callback_data=None):
    message = SimpleNamespace(reply_text=AsyncMock())
    query = None
    if callback_data is not None:
        query = SimpleNamespace(data=callback_data, answer=AsyncMock(),
                                edit_message_text=AsyncMock(), message=message)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(type=chat_type, id=1),
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=query,
    )


def _context(args=None):
    return SimpleNamespace(args=args or [], bot=SimpleNamespace(username="LogovoBot",
                                                                 send_document=AsyncMock()))


class TestOpsAccess:
    @pytest.mark.parametrize("handler", [
        admin_ops.cmd_health, admin_ops.cmd_backup, admin_ops.cmd_ocr_stats, admin_ops.cmd_audit,
    ])
    def test_non_global_admin_is_refused(self, handler, monkeypatch):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: False)
        collect = MagicMock()
        monkeypatch.setattr(admin_ops.bot_health, "collect", collect)
        update = _update()
        asyncio.run(handler(update, _context()))
        text = update.effective_message.reply_text.await_args.args[0]
        assert "Доступ запрещён" in text
        collect.assert_not_called()

    def test_non_global_admin_callback_gets_alert(self, monkeypatch):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: False)
        update = _update(callback_data="ops_backup:send:league-20260101-000000.db.gz")
        ctx = _context()
        asyncio.run(admin_ops.cb_backup(update, ctx))
        assert update.callback_query.answer.await_args.kwargs.get("show_alert") is True
        ctx.bot.send_document.assert_not_awaited()

    def test_group_chat_points_to_private_messages(self, monkeypatch):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: True)
        update = _update(chat_type="supergroup")
        asyncio.run(admin_ops.cmd_health(update, _context()))
        call = update.effective_message.reply_text.await_args
        assert "личных сообщениях" in call.args[0]
        button = call.kwargs["reply_markup"].inline_keyboard[0][0]
        assert button.url == "https://t.me/logovobot?start=health"

    def test_forged_backup_name_is_not_sent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: True)
        monkeypatch.setattr(admin_ops.config, "BACKUP_DIR", str(tmp_path))
        update = _update(callback_data="ops_backup:send:../league.db")
        ctx = _context()
        asyncio.run(admin_ops.cb_backup(update, ctx))
        ctx.bot.send_document.assert_not_awaited()
        assert "уже нет" in update.callback_query.answer.await_args.args[0]

    def test_audit_renders_for_global_admin(self, monkeypatch):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: True)
        database.log_admin_action(ADMIN_WITH_ROW, "warn_added", "user", 1, new_value="1/4")
        database.log_admin_action(OTHER_ADMIN, "round_closed", "round", 2)
        update = _update()
        asyncio.run(admin_ops.cmd_audit(update, _context(["@journaladmin"])))
        text = update.effective_message.reply_text.await_args.args[0]
        assert "Журнал админов" in text
        assert "Выдан варн" in text and "Закрыт тур" not in text
        assert f"<code>{ADMIN_WITH_ROW}</code>" in text

    def test_audit_unknown_user(self, monkeypatch):
        monkeypatch.setattr(admin_ops, "is_global_admin", lambda uid: True)
        update = _update()
        asyncio.run(admin_ops.cmd_audit(update, _context(["@ghost_user"])))
        assert "Не нашёл" in update.effective_message.reply_text.await_args.args[0]
