"""
tests/test_workshop.py

Тесты интерактивной мастерской графики ТО (/workshop, /studio, /cards).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram import Update, User, Chat, Message, CallbackQuery

import config
import database
from handlers import workshop


@pytest.fixture(autouse=True)
def _clean():
    with database.transaction() as conn:
        for t in ("transfers", "transfer_windows", "users"):
            conn.execute(f"DELETE FROM {t}")


def _user(user_id=1001, username="test_admin"):
    u = MagicMock(spec=User)
    u.id = user_id
    u.username = username
    return u


def _update(user_id=1001, is_callback=False, data="ws:menu"):
    up = MagicMock(spec=Update)
    user = _user(user_id)
    chat = MagicMock(spec=Chat)
    chat.type = "private"
    chat.id = user_id

    up.effective_user = user
    up.effective_chat = chat

    msg = MagicMock(spec=Message)
    msg.reply_text = AsyncMock()
    msg.reply_photo = AsyncMock()
    msg.delete = AsyncMock()
    up.effective_message = msg

    if is_callback:
        cq = MagicMock(spec=CallbackQuery)
        cq.data = data
        cq.answer = AsyncMock()
        cq.edit_message_text = AsyncMock()
        cq.delete_message = AsyncMock()
        cq.message = msg
        up.callback_query = cq
    else:
        up.callback_query = None

    return up


def test_permission_guard(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    monkeypatch.setattr(config, "TRANSFER_MANAGER_ID", 1002)

    assert workshop.is_workshop_allowed(1001) is True
    assert workshop.is_workshop_allowed(1002) is True
    assert workshop.is_workshop_allowed(9999) is False
    assert workshop.is_workshop_allowed(None) is False


def test_cmd_workshop_allowed(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001)
    ctx = MagicMock()

    asyncio.run(workshop.cmd_workshop(up, ctx))
    up.effective_message.reply_text.assert_awaited_once()
    assert "Мастерская графики ТО" in up.effective_message.reply_text.call_args.args[0]


def test_cmd_workshop_denied(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(9999)
    ctx = MagicMock()

    asyncio.run(workshop.cmd_workshop(up, ctx))
    up.effective_message.reply_text.assert_awaited_once()
    assert "Доступ запрещён" in up.effective_message.reply_text.call_args.args[0]


def test_cb_close(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:close")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.delete_message.assert_awaited_once()


def test_cb_menu(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:menu")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.edit_message_text.assert_awaited_once()


def test_cb_deal(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:deal")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.message.reply_photo.assert_awaited_once()
    kw = up.callback_query.message.reply_photo.call_args.kwargs
    assert "HERE WE GO" in kw["caption"]


def test_cb_surcharge(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:surcharge")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.message.reply_photo.assert_awaited_once()
    kw = up.callback_query.message.reply_photo.call_args.kwargs
    assert "СПЕЦКАРТА" in kw["caption"]


def test_cb_urn(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:urn")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.message.reply_photo.assert_awaited_once()
    kw = up.callback_query.message.reply_photo.call_args.kwargs
    assert "В УРНУ" in kw["caption"]


def test_cb_recap_demo(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:recap")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.message.reply_photo.assert_awaited_once()
    kw = up.callback_query.message.reply_photo.call_args.kwargs
    assert "итоги" in kw["caption"].lower()


def test_cb_from_db_empty(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_IDS", [1001])
    up = _update(1001, is_callback=True, data="ws:from_db")
    ctx = MagicMock()

    asyncio.run(workshop.cb_workshop(up, ctx))
    up.callback_query.answer.assert_awaited_once()
    up.callback_query.message.reply_text.assert_awaited_once()
    assert "В базе пока нет" in up.callback_query.message.reply_text.call_args.args[0]
