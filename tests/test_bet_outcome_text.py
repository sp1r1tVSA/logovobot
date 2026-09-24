"""
tests/test_bet_outcome_text.py

Человекочитаемые исходы ставок в /admin_bets: каждый ключ, который создаёт
odds_engine, должен превращаться в понятный текст, а не в сырой код.
"""

import pytest

from handlers.admin_bets import _event_label, _fmt_dt, _format_bet_card, _format_bet_snippet, _player_net
from services.bet_outcome_text import describe_selection, explain_result, goals_word

T1, T2 = "Кельн", "Айнтрахт"


@pytest.mark.parametrize("key, market, expected", [
    ("p1", "1x2", "Победит Кельн (П1)"),
    ("x", "1x2", "Ничья (X)"),
    ("draw", None, "Ничья (X)"),
    ("p2", "1x2", "Победит Айнтрахт (П2)"),
    ("1x", "double_chance", "Кельн не проиграет (1X)"),
    ("x2", "double_chance", "Айнтрахт не проиграет (X2)"),
    ("12", "double_chance", "Кто-то победит, без ничьей (12)"),
    ("over_1.5", "total_goals", "Тотал больше 1.5 гола (ТБ 1.5)"),
    ("under_2.5", "total_goals", "Тотал меньше 2.5 гола (ТМ 2.5)"),
    ("tb25", None, "Тотал больше 2.5 гола (ТБ 2.5)"),
    ("tm_3_5", None, "Тотал меньше 3.5 гола (ТМ 3.5)"),
    ("btts_yes", "btts", "Обе забьют — Да"),
    ("both_no", None, "Обе забьют — Нет"),
    ("it1_over_1.5", "individual_total_1", "Кельн забьёт больше 1.5 (Инд. тотал ИТБ1 1.5)"),
    ("it2_under_1.5", "individual_total_2", "Айнтрахт забьёт меньше 1.5 (Инд. тотал ИТМ2 1.5)"),
    ("h1_minus_1.5", "handicap", "Кельн победит с разницей 2+ мяча (Ф1 −1.5)"),
    ("h2_plus_1.5", "handicap", "Айнтрахт не проиграет с разницей 2+ мяча (Ф2 +1.5)"),
    ("cs_2_1", "correct_score", "Точный счёт 2:1"),
])
def test_describe_selection(key, market, expected):
    assert describe_selection(key, T1, T2, market_key=market) == expected


def test_unknown_key_falls_back_to_selection_name_then_raw_code():
    assert describe_selection("weird", T1, T2, selection_name="Что-то особое") == "Что-то особое"
    assert describe_selection("weird", T1, T2) == "weird"


@pytest.mark.parametrize("key, s1, s2, expected", [
    ("p2", 1, 3, "Счёт 1:3 — победа Айнтрахт"),
    ("x", 2, 2, "Счёт 2:2 — ничья"),
    ("under_2.5", 1, 3, "Счёт 1:3 — всего 4 гола, это больше 2.5"),
    ("over_1.5", 1, 0, "Счёт 1:0 — всего 1 гол, это меньше 1.5"),
    ("btts_yes", 1, 0, "Счёт 1:0 — Айнтрахт не забил"),
    ("btts_no", 0, 0, "Счёт 0:0 — никто не забил"),
    ("it1_over_1.5", 1, 3, "Счёт 1:3 — у Кельн 1 гол, это меньше 1.5"),
    ("h1_minus_1.5", 3, 1, "Счёт 3:1 — с учётом форы 1.5:1"),
])
def test_explain_result(key, s1, s2, expected):
    assert explain_result(key, T1, T2, s1, s2) == expected


def test_explain_result_without_score_is_none():
    assert explain_result("p1", T1, T2, None, None) is None


@pytest.mark.parametrize("n, word", [(1, "гол"), (2, "гола"), (5, "голов"), (11, "голов"), (21, "гол"), (0, "голов")])
def test_goals_word(n, word):
    assert goals_word(n) == word


def _bet(**overrides):
    bet = {
        "id": 74, "user_id": 1, "username": "brando055", "user_team": "Порту",
        "bet_type": "single", "status": "won", "amount": 100, "total_odd": 3.78,
        "potential_win": 378, "actual_payout": 378,
        "created_at": "2026-09-19 08:30:12", "settled_at": "2026-09-19 10:05:00",
        "items": [{
            "team1_name": T1, "team2_name": T2, "outcome_type": "p2", "market_key": "1x2",
            "odd": 3.78, "status": "won", "player1_score": 1, "player2_score": 3,
            "match_status": "confirmed", "tour": 5, "division_name": "Дивизион 2",
        }],
    }
    bet.update(overrides)
    return bet


def test_snippet_shows_readable_pick_score_net_and_settle_time():
    text = _format_bet_snippet(_bet())
    assert "Кельн 1:3 Айнтрахт" in text
    assert "Победит Айнтрахт (П2)" in text
    assert "+278 🪙" in text
    assert "Поставлена 19.09 08:30 · рассчитана 19.09 10:05 (МСК)" in text
    assert "p2" not in text


def test_snippet_pending_bet_marks_unplayed_match():
    item = dict(_bet()["items"][0], status="pending", player1_score=None, player2_score=None, match_status="pending")
    text = _format_bet_snippet(_bet(status="pending", actual_payout=0, settled_at=None, items=[item]))
    assert "не сыгран" in text
    assert "ждёт расчёта" in text
    assert "возможный выигрыш" in text


@pytest.mark.parametrize("status, payout, expected", [
    ("won", 378, 278), ("won", 0, 278), ("lost", 0, -100),
    ("refunded", 0, 0), ("cashed_out", 150, 50), ("pending", 0, None),
])
def test_player_net(status, payout, expected):
    assert _player_net(_bet(status=status, actual_payout=payout)) == expected


def test_card_contains_breakdown_player_stats_and_cashout():
    stats = {
        "total_bets": 10, "count_pending": 2, "count_won": 3, "count_lost": 3,
        "count_refunded": 1, "count_cashed_out": 1, "total_wagered": 1000,
        "pending_amount": 200, "total_paid_out": 900, "settled_wagered": 700,
        "net_profit": 200, "win_rate": 50.0,
    }
    card = _format_bet_card(_bet(status="cashed_out", actual_payout=150, cashout_at="2026-09-19 09:00:00"), stats)
    assert "Счёт 1:3 — победа Айнтрахт" in card
    assert "✅ зашло" in card
    assert "Процент побед: <b>50.0%</b>" in card
    assert "+200 🪙" in card
    assert "Забрал: <b>150 🪙</b> из 378 🪙" in card
    assert "недополучил 228 🪙" in card
    assert "Поставлена:</b> 19.09 08:30 МСК" in card
    assert "Кэшаут сделан:</b> 19.09 09:00 МСК" in card


@pytest.mark.parametrize("raw, expected", [
    # В базе лежит московское время (time_utils) — показываем как есть.
    ("2026-09-19 08:42:11", "19.09 08:42"),
    ("2026-09-19 22:15:00", "19.09 22:15"),
    ("2026-12-31 23:30", "31.12 23:30"),
    # А вот строка с зоной приходит из внешнего источника — её переводим в МСК.
    ("2026-09-19T08:42:11Z", "19.09 11:42"),
    ("", "—"),
    (None, "—"),
    ("вчера", "вчера"),
])
def test_fmt_dt_shows_stored_moscow_time(raw, expected):
    assert _fmt_dt(raw) == expected


# ─── Откуда матч: тур лиги, кубок дивизиона, общий кубок ─────────────────────
@pytest.mark.parametrize("item, expected", [
    ({"tournament_type": "league", "division_name": "Дивизион 2", "tour": 5}, "🏟 Дивизион 2 · Тур 5"),
    ({"tour": 3}, "🏟 Тур 3"),
    ({"tournament_type": "cup", "cup_stage": "1/8", "cup_stage_key": "1/8@D3", "cup_division_id": 3,
      "cup_division_code": "DIV_3", "cup_division_name": "Дивизион 3", "game_num_in_series": 2},
     "🏆 Кубок Д3 · 1/8 · игра 2"),
    ({"tournament_type": "cup", "cup_stage": "1/4", "cup_stage_key": "1/4", "game_num_in_series": 1},
     "🏆 Общий кубок · 1/4 · игра 1"),
    ({"tournament_type": "cup", "cup_stage_key": "1/8@D4", "is_series_header": 1, "game_num_in_series": 1},
     "🏆 Кубок Д4 · 1/8 · исход серии"),
    ({"tournament_type": "cup", "cup_stage": "Финал", "cup_division_id": 7,
      "cup_division_code": "DIV_XK2", "cup_division_name": "Дивизион 6"},
     "🏆 Кубок Дивизион 6 · Финал"),
    ({"tournament_type": "friendly"}, "🤝 Товарищеский матч"),
])
def test_event_label_names_the_competition(item, expected):
    assert _event_label(item) == expected


NO_BETS = {k: 0 for k in (
    "total_bets", "count_pending", "count_won", "count_lost", "count_refunded",
    "count_cashed_out", "total_wagered", "pending_amount", "total_paid_out", "net_profit",
)}


def test_snippet_and_card_say_where_the_match_is_played():
    cup_leg = dict(_bet()["items"][0], tournament_type="cup", cup_stage="1/8",
                   cup_stage_key="1/8", game_num_in_series=3)
    bet = _bet(items=[_bet()["items"][0], cup_leg])
    for text in (_format_bet_snippet(bet), _format_bet_card(bet, NO_BETS)):
        assert "Дивизион 2 · Тур 5" in text
        assert "Общий кубок · 1/8 · игра 3" in text
