import pytest
import sqlite3
import database
from services.betting_engine import _get_team_strength_score, calculate_match_odds

def test_team_strength_uses_preseason_seeds():
    # Standings are empty (0 games played)
    standings = []
    
    # In config.DIVISION_PLAYER_SEEDS DIV_1:
    # @ch1lyx is position 15 (seed 1.0, strongest)
    # @Saharokk8830 is position 0 (seed 0.0, weakest)
    score_p1 = _get_team_strength_score(standings, "Porto", nickname="@ch1lyx")
    score_p2 = _get_team_strength_score(standings, "Benfica", nickname="@Saharokk8830")
    
    # Top seed should have higher strength than lower seed
    assert score_p1 == 15.0
    assert score_p2 == 5.0
    assert score_p1 > score_p2

    # Odds for @ch1lyx vs @Saharokk8830 should not be identical 2.32 / 2.44
    odds = calculate_match_odds(
        "Porto",
        "Benfica",
        p1_nick="@ch1lyx",
        p2_nick="@Saharokk8830"
    )
    assert odds["odd_p1"] < odds["odd_p2"], f"Favorite should have lower odds than underdog, got {odds}"
    assert odds["odd_p1"] < 1.6, f"Heavy favorite should have low odds, got {odds['odd_p1']}"
    assert odds["odd_p2"] > 3.5, f"Underdog should have high odds, got {odds['odd_p2']}"


def test_get_cabinet_matches_only_open_rounds(tmp_path, monkeypatch):
    db_file = tmp_path / "test_league.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    database.init_db()

    with database.transaction() as conn:
        c = conn.cursor()
        # Create user
        c.execute("""
            INSERT INTO users (telegram_id, username, team_name, division_id)
            VALUES (1001, 'player1', 'Arsenal', 1)
        """)
        c.execute("""
            INSERT INTO users (telegram_id, username, team_name, division_id)
            VALUES (1002, 'player2', 'Chelsea', 1)
        """)
        
        # Round 1 is OPEN, Round 2 is CLOSED
        c.execute("INSERT INTO rounds (round_number, division_id, is_open) VALUES (1, 1, 1)")
        c.execute("INSERT INTO rounds (round_number, division_id, is_open) VALUES (2, 1, 0)")

        # Match 1 in Round 1 (open), Match 2 in Round 2 (closed)
        c.execute("""
            INSERT INTO matches (round_number, division_id, player1_team, player2_team, status)
            VALUES (1, 1, 'Arsenal', 'Chelsea', 'pending')
        """)
        c.execute("""
            INSERT INTO matches (round_number, division_id, player1_team, player2_team, status)
            VALUES (2, 1, 'Arsenal', 'Chelsea', 'pending')
        """)

    # When querying matches for Arsenal coach (telegram_id=1001)
    matches = database.get_cabinet_matches(1001)
    
    # Should only return Round 1 match! Round 2 must be hidden.
    round_numbers = [m["round_number"] for m in matches]
    assert 1 in round_numbers
    assert 2 not in round_numbers
    assert len(matches) == 1


def test_cabinet_history_labels_cup_games(tmp_path, monkeypatch):
    """Кубковая игра в истории «Мой клуб» — этап и номер игры, а не «Тур -1»."""
    db_file = tmp_path / "test_league.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_file))
    database.init_db()

    with database.transaction() as conn:
        c = conn.cursor()
        c.execute("""
            INSERT INTO users (telegram_id, username, team_name, division_id)
            VALUES (1001, 'player1', 'Arsenal', 1)
        """)
        c.execute("INSERT INTO rounds (round_number, division_id, is_open, deadline) VALUES (1, 1, 1, '2026-09-30 23:59')")
        c.execute("""
            INSERT INTO matches (round_number, division_id, player1_team, player2_team,
                                 player1_score, player2_score, status, played_at)
            VALUES (1, 1, 'Arsenal', 'Chelsea', 2, 0, 'confirmed', '2026-09-20 20:00:00')
        """)
        c.execute("""
            INSERT INTO matches (round_number, division_id, tournament_type, cup_stage, game_num_in_series,
                                 player1_team, player2_team, player1_score, player2_score, status, played_at)
            VALUES (-1, 0, 'cup', '1/64', 2, 'Chelsea', 'Arsenal', 2, 3, 'confirmed', '2026-09-23 20:00:00')
        """)

    cup, league = database.get_cabinet_recent_matches(1001)

    assert cup["is_cup"] is True
    assert (cup["cup_stage"], cup["game_num"]) == ("1/64", 2)
    assert cup["deadline"] is None  # у кубка нет тура — нет и дедлайна тура
    assert cup["score"] == "3 : 2" and cup["is_home"] is False

    assert league["is_cup"] is False
    assert league["cup_stage"] is None and league["game_num"] is None
    assert league["deadline"] == "2026-09-30 23:59"
